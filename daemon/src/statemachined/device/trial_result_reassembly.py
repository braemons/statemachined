# SPDX-License-Identifier: LGPL-3.0-or-later
"""Reassembling `result_begin` … `result_path`* … `result_end`.

Was tests/hardware/hardware_test_harness.py, for the reason graph_set_upload.py records. Lines are
parsed here rather than through `RequestResponseSession.request`, for the checksum: the device
folds the covered bytes of every result line before `result_end`, so the host
has to fold the same bytes, and a parsed dict has already thrown them away.
"""

from __future__ import annotations

import time

from .message_vocabulary import Field, MsgType
from .request_response_session import RequestResponseSession
from .message_framing import CRC_INIT, covered_bytes, crc16_ccitt, parse_reply


class StateVisitRow:
    """One row of `result_path`, named. The wire form is a bare array.

    The same six-element shape the `visit` stream will carry, decoded by the
    same class -- two shapes for one fact is how the two drift apart.
    """

    __slots__ = ("state_index", "cause", "transition_index", "drawn_ms", "entered_us",
                 "duration_us")

    def __init__(self, row: list):
        (
            self.state_index,
            self.cause,
            self.transition_index,
            self.drawn_ms,
            self.entered_us,
            self.duration_us,
        ) = row


class ReassembledTrialResult:
    """A trial's result, reassembled and checked against its own checksum."""

    def __init__(self, begin: dict, rows: list[list], end: dict, computed: int):
        self.begin = begin
        self.rows = rows
        self.end = end
        self.computed = computed

    @property
    def trial_id(self) -> int:
        return self.begin["trial_id"]

    @property
    def outcome(self) -> int:
        return self.begin["outcome"]

    @property
    def checksum_matches(self) -> bool:
        return self.end.get("checksum", "").upper() == f"{self.computed:04X}"

    def visit(self, n: int) -> StateVisitRow:
        return StateVisitRow(self.rows[n])


class TrialResultCollector:
    """Fed one line at a time, and finished when the last chunk arrives.

    A collector rather than a loop, because a daemon does not get to sit in one.
    A result arrives unasked, in the middle of whatever else the link is doing,
    and the thread that reads the link has to keep reading -- so the state of a
    half-assembled result lives here instead of on somebody's stack.

    Ordering is checked as strictly as the protocol states it: a chunk that
    overtook its `result_begin` would break the fold silently.
    """

    def __init__(self) -> None:
        self._begin: dict | None = None
        self._rows: list[list] = []
        self._rolling_checksum = CRC_INIT

    @property
    def is_assembling(self) -> bool:
        return self._begin is not None

    def feed(self, line: str, message: dict) -> ReassembledTrialResult | None:
        """Take one already-parsed line. Returns the result when it completes.

        Both the raw line and the parsed message, because the rolling checksum
        is over *bytes* -- the device folded the covered span of every result
        line before `result_end`, and a parsed dict has already thrown them
        away.
        """
        message_type = message.get(Field.MSG_TYPE)

        if message_type == MsgType.RESULT_BEGIN:
            self._begin = message
            self._rows = []
            self._rolling_checksum = crc16_ccitt(covered_bytes(line), CRC_INIT)
            return None

        if message_type == MsgType.RESULT_PATH:
            if self._begin is None:
                raise ValueError("result_path arrived before result_begin")
            if message["from"] != len(self._rows):
                raise ValueError(
                    f"result_path starts at {message['from']}, but {len(self._rows)} rows "
                    "have arrived: a chunk is missing or out of order"
                )
            self._rolling_checksum = crc16_ccitt(covered_bytes(line), self._rolling_checksum)
            self._rows.extend(message["p"])
            return None

        if message_type == MsgType.RESULT_END:
            if self._begin is None:
                raise ValueError("result_end arrived before result_begin")
            # Deliberately not folded: it carries the checksum.
            result = ReassembledTrialResult(
                self._begin, self._rows, message, self._rolling_checksum
            )
            self._begin = None
            self._rows = []
            return result

        return None


def read_trial_result(
    session: RequestResponseSession, timeout: float = 15.0
) -> ReassembledTrialResult:
    """Sit and collect one whole result. For callers that have nothing else to do.

    The bench and the tests. A daemon uses `TrialResultCollector` from whichever
    thread owns the link, because it has other things to answer meanwhile.

    Messages that are not part of the result go to the session's unsolicited
    sink rather than being dropped: a `log` at error level during a trial is
    evidence.
    """
    collector = TrialResultCollector()
    deadline = time.monotonic() + timeout

    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"the result did not arrive whole within {timeout:g}s "
                f"(assembling={collector.is_assembling})"
            )
        line = session.link.read_line()
        if line is None:
            continue
        message = parse_reply(line)  # a FramingError here is a finding, not a retry
        result = collector.feed(line, message)
        if result is not None:
            return result
        if message.get(Field.MSG_TYPE) not in (
            MsgType.RESULT_BEGIN,
            MsgType.RESULT_PATH,
            MsgType.RESULT_END,
        ):
            session.on_unsolicited(message, line)
