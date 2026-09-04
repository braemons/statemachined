# SPDX-License-Identifier: GPL-3.0-or-later
"""What the hardware tests need beyond the daemon's own modules.

The graph upload and the result reassembly that used to live here are now
`statemachined.device.graph_set_upload` and `.result`, and this suite imports them --
which is the whole point of the move: these tests now check the daemon's codec
against real hardware instead of a copy of it. What is left here is what is
genuinely test-only: raw-line access, deliberate refusals, and the enums a
bench instrument has no use for.
"""

from __future__ import annotations

import time
from enum import IntEnum

from statemachined.device.serial_link import SerialLink
from statemachined.device.message_vocabulary import Field, MsgType
from statemachined.device.trial_result_reassembly import ReassembledTrialResult, StateVisitRow, read_trial_result  # noqa: F401  re-exported
from statemachined.device.request_response_session import RequestResponseSession
from statemachined.device.graph_set_upload import SingleGraphSetUploader  # noqa: F401  re-exported
from statemachined.device.message_framing import (  # noqa: F401  re-exported
    CRC_INIT,
    DeviceRefusedTheCommand,
    covered_bytes,
    parse_reply,
)


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

    Here rather than in `statemachined.device.message_vocabulary` because the CLI has no
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


class Device:
    """One greeted board, with the reads the tests need.

    Wraps `RequestResponseSession` rather than replacing it: the request/reply rules, the
    unsolicited routing and the no-retry policy are the CLI's and are not
    re-decided here. What this adds is the ability to see *raw lines*, which
    `RequestResponseSession` hides -- a result's rolling checksum is over bytes, so a test
    that only saw parsed dicts could not check it.
    """

    def __init__(self, link: SerialLink, session: RequestResponseSession):
        self.link = link
        self.session = session
        #: Unsolicited messages that arrived while waiting for a reply. Kept
        #: rather than dropped: a `log` at `error` level during a test is
        #: evidence, even when the test it arrived during passed.
        self.stray: list[dict] = []
        self.junk: list[tuple[str, str]] = []
        session.on_unsolicited = lambda message, line: self.stray.append(message)
        session.on_junk = lambda line, why: self.junk.append((line, why))

    # ----------------------------------------------------------- commands ---

    def request(self, msg_type: MsgType, timeout: float = 5.0, **fields) -> dict:
        return self.session.request(msg_type, timeout=timeout, **fields)

    def state(self, timeout: float = 5.0) -> dict:
        return self.session.state(timeout=timeout)

    def refuse(self, msg_type: MsgType, timeout: float = 5.0, **fields) -> DeviceRefusedTheCommand:
        """Send a command that must be refused, and return the refusal.

        A command that is *answered* fails the test here rather than three
        assertions later, because "the device accepted it" and "the device
        refused it for the wrong reason" are different findings and only one of
        them means the guard is missing.
        """
        try:
            reply = self.request(msg_type, timeout=timeout, **fields)
        except DeviceRefusedTheCommand as exc:
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
        `in_reply_to` at all (PROTOCOL.md §5). `RequestResponseSession.request` cannot be used
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
