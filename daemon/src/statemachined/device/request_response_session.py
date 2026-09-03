# SPDX-License-Identifier: LGPL-3.0-or-later
"""One command out, one reply back, and everything else handed to a sink.

Strict request/response with a single command in flight, which is what the
protocol's one-deep duplicate memory assumes (PROTOCOL.md §1.2). Unsolicited
messages -- `event`, `log`, `result_*` -- can arrive between a command and its
reply, so the read loop matches on `in_reply_to` rather than on arrival order.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from .serial_link import SerialLink
from .message_vocabulary import UNSOLICITED, Field, MsgType
from .message_framing import DeviceRefusedTheCommand, FramingError, command_line, parse_reply

PROTOCOL_VERSION = 1


class NoReplyInTime(Exception):
    """No reply carrying our `in_reply_to` arrived before the deadline."""


def random_seed() -> str:
    """A session seed as `hex64`: 16 hex digits, never a JSON number.

    64 bits do not survive a double, and a seed that silently changes is a
    reproducibility bug nobody would find.
    """
    return f"{random.getrandbits(64):016X}"


class RequestResponseSession:
    def __init__(
        self,
        link: SerialLink,
        on_unsolicited: Callable[[dict], None] | None = None,
        on_junk: Callable[[str, str], None] | None = None,
    ):
        self.link = link
        # Counting from 1, not 0. A message_id of 0 is perfectly legal -- the
        # counter is a u16 that wraps through it -- and current firmware
        # answers it like any other. Firmware built before the
        # send_orphan_error split read 0 as "no id could be read" and refused
        # such a command without naming it, and boards in a rack are flashed
        # when somebody gets to them, so this stays: it costs nothing and keeps
        # a session's first command out of that hole. The unattributed reply is
        # still handled below, for the wrap and for a device with its own
        # version of the same bug.
        self.message_id = 1
        self.on_unsolicited = on_unsolicited or (lambda msg: None)
        self.on_junk = on_junk or (lambda line, why: None)
        self.hello_ack: dict | None = None

    def _next_message_id(self) -> int:
        message_id = self.message_id
        self.message_id = (self.message_id + 1) & 0xFFFF  # u16, wrapping through zero
        return message_id

    @staticmethod
    def _answers(msg: dict, message_id: int) -> bool:
        """Is this the reply to the command with that message_id?

        `in_reply_to` is the answer when it is there. An `error` without one is
        taken as the reply anyway: the protocol requires `in_reply_to` on every
        reply, but a refusal that arrives while exactly one command is
        outstanding is about that command whatever it is labelled, and
        swallowing it would turn a clear "no hello yet" into a five-second
        silence.
        """
        if Field.IN_REPLY_TO in msg:
            return msg[Field.IN_REPLY_TO] == message_id
        return msg.get(Field.MSG_TYPE) == MsgType.ERROR

    def request(self, msg_type: MsgType, timeout: float = 5.0, **fields) -> dict:
        """Send one command and return its reply, raising on `error`.

        No retry. The protocol makes a blind resend safe, but a retry that a
        person did not ask for turns "the board did not answer" into "the board
        answered eventually", and that is exactly the fact §5 is measuring.
        """
        message_id = self._next_message_id()
        line = command_line(msg_type, message_id, **fields)
        return self.request_line(line, message_id, timeout=timeout, what=str(msg_type))

    def request_line(
        self, line: str, message_id: int, timeout: float = 5.0, what: str = "the command"
    ) -> dict:
        """The same, for a command already framed by the caller.

        A graph upload folds a rolling checksum over the exact bytes it sent
        (PROTOCOL.md §3.2), so it has to build the line itself and cannot let
        `request` build one it never sees. Everything below the framing --
        matching on `in_reply_to`, routing the unsolicited aside, raising on a
        refusal, not retrying -- is the same and is not re-decided there.
        """
        self.link.write_line(line)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NoReplyInTime(
                    f"no reply to {what} (message_id {message_id}) within {timeout:g}s"
                )
            raw = self.link.read_line()
            if raw is None:
                continue
            msg = self.receive(raw)
            if msg is None:
                continue
            if self._answers(msg, message_id):
                if msg.get(Field.MSG_TYPE) == MsgType.ERROR:
                    if Field.IN_REPLY_TO not in msg:
                        self.on_junk(
                            raw, "an error naming no message_id; taken as the reply anyway"
                        )
                    raise DeviceRefusedTheCommand(msg)
                return msg
            # A reply to somebody else's command, or one whose id we already
            # gave up on. Worth seeing rather than swallowing.
            self.on_junk(
                raw,
                f"reply to message_id {msg.get(Field.IN_REPLY_TO)}, expected {message_id}",
            )

    def receive(self, line: str) -> dict | None:
        """Parse one received line, routing what is not a reply."""
        if not line.strip():
            return None
        try:
            msg = parse_reply(line)
        except FramingError as exc:
            self.on_junk(line, str(exc))
            return None
        if msg.get(Field.MSG_TYPE) in UNSOLICITED:
            self.on_unsolicited(msg)
            return None
        return msg

    def hello(self, seed: str | None = None, timeout: float = 5.0) -> dict:
        """Open a session -- and, on a bench board, end demo mode for good.

        Opening the port does not do that; the greeting does. It is the whole
        point of the handover that a serial monitor cannot trigger it.
        """
        ack = self.request(
            MsgType.HELLO, timeout=timeout, proto=PROTOCOL_VERSION, seed=seed or random_seed()
        )
        self.hello_ack = ack
        return ack

    def state(self, timeout: float = 5.0) -> dict:
        return self.request(MsgType.STATE, timeout=timeout)

    def ping(self, timeout: float = 5.0) -> dict:
        return self.request(MsgType.PING, timeout=timeout)
