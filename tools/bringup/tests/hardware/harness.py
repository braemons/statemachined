# SPDX-License-Identifier: GPL-3.0-or-later
"""What the hardware tests need beyond the CLI's own modules.

Two things live here rather than in `statemachined_bringup`: a graph upload,
and reading a result back. Both are things the *bridge* does, not things a
bench instrument does, and tools/bringup/README.md says plainly that anything
which starts to look like it is running an experiment belongs in `bridge/`. So
they stay in the tests, where the only paradigm they run is the two-state graph
this suite uses to time a state.
"""

from __future__ import annotations

import time
from enum import IntEnum

from statemachined_bringup.link import Link
from statemachined_bringup.messages import Field, MsgType
from statemachined_bringup.session import Session
from statemachined_bringup.wire import DeviceError, command_line, crc16_ccitt, parse_reply

#: The rolling checksums in this protocol -- the graph upload's and the
#: result's -- both start here and both fold the CRC-covered bytes of each
#: line. See dev/PROTOCOL.md §3.
CRC_INIT = 0xFFFF

_CRC_MEMBER = ',"crc":'


class LinkState(IntEnum):
    """`state_report.link_state`, mirroring LinkState in host_link_session.h.

    On the wire it is the enum's number, not its name, which is why a reader
    needs this table at all.
    """

    GREETING = 0  # no hello yet; only hello is accepted
    IDLE = 1  # greeted; a graph may or may not be committed
    ARMED = 2  # configured for a trial that has not started
    RUNNING = 3  # a trial is in flight


class Outcome(IntEnum):
    """`result_begin.outcome`, mirroring TrialOutcome in firmware/core/trial/trial.h.

    Here rather than in `statemachined_bringup.messages` because the CLI has no
    use for it: a bench instrument reads pins and scan health, and the moment it
    starts interpreting how a trial ended it has become the bridge. The tests
    need the names, so the tests keep them.
    """

    UNDETERMINED = -1
    NOT_STARTED = 0
    HIT = 1
    CANCELLED = 10


class CancelReason(IntEnum):
    """`result_begin.cancel_reason`, mirroring TrialCancelReason."""

    NONE = 0
    HOST = 1
    LINK_LOST = 2
    ABORT_LINE = 3
    TRIAL_TIMEOUT = 4


def covered_bytes(line: str) -> bytes:
    """The part of a line a rolling checksum folds: everything before `crc`.

    The same span the device's own CRC covers, which is what makes the two
    checksums comparable at all.
    """
    return line[: line.rindex(_CRC_MEMBER)].encode("ascii")


class Device:
    """One greeted board, with the reads the tests need.

    Wraps `Session` rather than replacing it: the request/reply rules, the
    unsolicited routing and the no-retry policy are the CLI's and are not
    re-decided here. What this adds is the ability to see *raw lines*, which
    `Session` hides -- a result's rolling checksum is over bytes, so a test
    that only saw parsed dicts could not check it.
    """

    def __init__(self, link: Link, session: Session):
        self.link = link
        self.session = session
        #: Unsolicited messages that arrived while waiting for a reply. Kept
        #: rather than dropped: a `log` at `error` level during a test is
        #: evidence, even when the test it arrived during passed.
        self.stray: list[dict] = []
        self.junk: list[tuple[str, str]] = []
        session.on_unsolicited = self.stray.append
        session.on_junk = lambda line, why: self.junk.append((line, why))

    # ----------------------------------------------------------- commands ---

    def request(self, msg_type: MsgType, timeout: float = 5.0, **fields) -> dict:
        return self.session.request(msg_type, timeout=timeout, **fields)

    def state(self, timeout: float = 5.0) -> dict:
        return self.session.state(timeout=timeout)

    def refuse(self, msg_type: MsgType, timeout: float = 5.0, **fields) -> DeviceError:
        """Send a command that must be refused, and return the refusal.

        A command that is *answered* fails the test here rather than three
        assertions later, because "the device accepted it" and "the device
        refused it for the wrong reason" are different findings and only one of
        them means the guard is missing.
        """
        try:
            reply = self.request(msg_type, timeout=timeout, **fields)
        except DeviceError as exc:
            return exc
        raise AssertionError(f"{msg_type} was accepted, not refused: {reply}")

    def send_line(self, line: str) -> None:
        """Put a line on the wire exactly as given, framing and all."""
        self.link.write_line(line)

    def exchange(self, line: str, timeout: float = 5.0) -> dict:
        """Send an exact line and return the reply, whatever it turns out to be.

        For the tests that build their own line -- a deliberately corrupt one, a
        `message_id` the CLI's counter will not produce, the same bytes sent
        twice. Unlike `request` it asserts nothing about what comes back, and
        unlike `read_error` it routes unsolicited traffic aside on the way.
        """
        self.send_line(line)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self.link.read_line()
            if raw is None:
                continue
            msg = self.session.receive(raw)
            if msg is not None:
                return msg
        raise AssertionError(f"no reply within {timeout:g}s to: {line}")

    def read_error(self, timeout: float = 5.0) -> dict:
        """The next `error`, however it is labelled.

        For lines the device could not attribute to a `message_id` -- a bad
        CRC, an over-long line -- which it answers with an `error` carrying no
        `in_reply_to` at all (PROTOCOL.md §5). `Session.request` cannot be used
        for those, since there is no id to match on.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.link.read_line()
            if line is None:
                continue
            msg = parse_reply(line)
            if msg.get(Field.MSG_TYPE) == MsgType.ERROR:
                return msg
            self.stray.append(msg)
        raise AssertionError(f"no error within {timeout:g}s")

    def next_message_id(self) -> int:
        return self.session._next_message_id()

    def settle(self, seconds: float = 0.3) -> list[dict]:
        """Read whatever is still coming, and return it.

        Between tests, so that one test's trailing `result_path` is not the
        next test's mysterious extra line.
        """
        out = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            line = self.link.read_line()
            if line is None:
                continue
            try:
                out.append(parse_reply(line))
            except Exception:  # noqa: BLE001 -- recorded, not acted on
                pass
        return out


# ------------------------------------------------------------------ graph ---


class GraphUpload:
    """A graph, sent message by message, with the rolling checksum kept.

    The checksum is the point. `graph_end` carries a CRC-16 over the covered
    bytes of every graph message before it, so a chunk that went missing is
    caught even though the line that vanished was perfectly well formed -- and
    computing it here, from the bytes this side actually sent, is the only way
    a test can tell that the device folded the same ones.

    `n_transitions` and `n_output_actions` are counted rather than passed in,
    because a test that has to state its own totals is a test that will one day
    state them wrongly and blame the firmware.
    """

    def __init__(self, device: Device, version: int = 1):
        self.device = device
        self.version = version
        self.rolling = CRC_INIT
        self.n_transitions = 0
        self.n_output_actions = 0

    def _send(self, msg_type: MsgType, **fields) -> dict:
        message_id = self.device.next_message_id()
        line = command_line(msg_type, message_id, **fields)
        self.rolling = crc16_ccitt(covered_bytes(line), self.rolling)
        self.device.send_line(line)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            raw = self.device.link.read_line()
            if raw is None:
                continue
            reply = self.device.session.receive(raw)
            if reply is None:
                continue
            if reply.get(Field.MSG_TYPE) == MsgType.ERROR:
                raise DeviceError(reply)
            return reply
        raise AssertionError(f"no reply to {msg_type}")

    def begin(self, n_states: int, entry: int = 0) -> dict:
        return self._send(
            MsgType.GRAPH_BEGIN, graph_version=self.version, n_states=n_states, entry=entry
        )

    def dist(self, i: int, kind: str = "fixed", **fields) -> dict:
        return self._send(MsgType.GRAPH_DIST, i=i, kind=kind, **fields)

    def state(self, i: int, terminal: int | None = None, timeout: dict | None = None) -> dict:
        # Both members always present, `terminal` as null where the state is not
        # terminal. The protocol distinguishes absent from null and the device
        # refuses the former, rather than guessing which a graph meant.
        return self._send(MsgType.GRAPH_STATE, i=i, terminal=terminal, timeout=timeout)

    def transition(self, target: int, **fields) -> dict:
        self.n_transitions += 1
        return self._send(MsgType.GRAPH_TRANSITION, target=target, **fields)

    def action(self, on: str, line: int, kind: str = "high") -> dict:
        self.n_output_actions += 1
        return self._send(MsgType.GRAPH_ACTION, on=on, line=line, kind=kind)

    def end(self, checksum: str | None = None) -> dict:
        return self._send(
            MsgType.GRAPH_END,
            n_transitions=self.n_transitions,
            n_output_actions=self.n_output_actions,
            checksum=checksum if checksum is not None else f"{self.rolling:04X}",
        )


# ----------------------------------------------------------------- result ---


class Result:
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

    def visit(self, n: int) -> "Visit":
        return Visit(self.rows[n])


class Visit:
    """One row of `result_path`, named. The wire form is a bare array."""

    def __init__(self, row: list):
        (
            self.state_index,
            self.cause,
            self.transition_index,
            self.drawn_ms,
            self.entered_us,
            self.duration_us,
        ) = row


def read_result(device: Device, timeout: float = 15.0) -> Result:
    """Collect `result_begin` … `result_path`* … `result_end`.

    Lines are parsed here rather than through `Session`, for the checksum: the
    device folds the covered bytes of every result line before `result_end`, so
    the host has to fold the same bytes, and a parsed dict has already thrown
    them away. Ordering is checked as strictly as the protocol states it --
    a chunk that overtook its `result_begin` would break the fold silently.
    """
    begin = end = None
    rows: list[list] = []
    rolling = CRC_INIT
    deadline = time.monotonic() + timeout

    while end is None:
        if time.monotonic() > deadline:
            raise AssertionError(
                f"the result did not arrive whole within {timeout:g}s "
                f"(begin={begin is not None}, {len(rows)} rows, no result_end)"
            )
        line = device.link.read_line()
        if line is None:
            continue
        msg = parse_reply(line)  # a WireError here is a finding, not a retry
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
            device.stray.append(msg)

    return Result(begin, rows, end, rolling)
