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


def read_trial_result(session: RequestResponseSession, timeout: float = 15.0) -> ReassembledTrialResult:
    """Collect one whole result off the link.

    Ordering is checked as strictly as the protocol states it -- a chunk that
    overtook its `result_begin` would break the fold silently. Messages that
    are not part of the result go to the session's unsolicited sink rather than
    being dropped: a `log` at error level during a trial is evidence.
    """
    begin = end = None
    rows: list[list] = []
    rolling = CRC_INIT
    deadline = time.monotonic() + timeout

    while end is None:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"the result did not arrive whole within {timeout:g}s "
                f"(begin={begin is not None}, {len(rows)} rows, no result_end)"
            )
        line = session.link.read_line()
        if line is None:
            continue
        msg = parse_reply(line)  # a FramingError here is a finding, not a retry
        msg_type = msg.get(Field.MSG_TYPE)

        if msg_type == MsgType.RESULT_BEGIN:
            assert begin is None, "a second result_begin arrived"
            rolling = crc16_ccitt(covered_bytes(line), rolling)
            begin = msg
        elif msg_type == MsgType.RESULT_PATH:
            assert begin is not None, "result_path arrived before result_begin"
            assert msg["from"] == len(rows), (
                f"result_path starts at {msg['from']}, but {len(rows)} rows have arrived: "
                "a chunk is missing or out of order"
            )
            rolling = crc16_ccitt(covered_bytes(line), rolling)
            rows.extend(msg["p"])
        elif msg_type == MsgType.RESULT_END:
            assert begin is not None, "result_end arrived before result_begin"
            end = msg  # deliberately not folded: it carries the checksum
        else:
            session.on_unsolicited(msg)

    return ReassembledTrialResult(begin, rows, end, rolling)
