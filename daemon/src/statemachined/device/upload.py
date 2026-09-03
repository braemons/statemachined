# SPDX-License-Identifier: LGPL-3.0-or-later
"""A graph, sent message by message, with the rolling checksum kept.

Was tests/hardware/harness.py, which said of it: *"Both are things the bridge
does, not things a bench instrument does… they stay in the tests"* -- and
daemon/README.md, that anything which starts to look like it is running
an experiment belongs in the bridge. The bridge is this package now, so it does.
The hardware suite imports it from here, which makes that suite a test of the
daemon's codec against real hardware rather than of a copy of it.

The checksum is the point. `graph_end` carries a CRC-16 over the covered bytes
of every graph message before it (PROTOCOL.md §3.2), so a chunk that went
missing is caught even though the line that vanished was perfectly well formed
-- and computing it from the bytes this side actually sent is the only way to
tell that the device folded the same ones.
"""

from __future__ import annotations

from .messages import MsgType
from .session import Session
from .wire import CRC_INIT, command_line, covered_bytes, crc16_ccitt


class GraphUpload:
    """One `graph_begin` … `graph_end`, against one greeted device.

    `n_transitions` and `n_output_actions` are counted rather than passed in,
    because a caller that has to state its own totals is a caller that will one
    day state them wrongly and blame the firmware.
    """

    def __init__(self, session: Session, version: int = 1, timeout: float = 5.0):
        self.session = session
        self.version = version
        self.timeout = timeout
        self.rolling = CRC_INIT
        self.n_transitions = 0
        self.n_output_actions = 0

    def _send(self, msg_type: MsgType, **fields) -> dict:
        message_id = self.session._next_message_id()
        line = command_line(msg_type, message_id, **fields)
        self.rolling = crc16_ccitt(covered_bytes(line), self.rolling)
        return self.session.request_line(
            line, message_id, timeout=self.timeout, what=str(msg_type)
        )

    def begin(self, n_states: int, entry: int = 0, **fields) -> dict:
        return self._send(
            MsgType.GRAPH_BEGIN,
            graph_version=self.version,
            n_states=n_states,
            entry=entry,
            **fields,
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
        """Close the upload. The device commits only if its fold agrees.

        `checksum` is an override for the tests that send a wrong one on
        purpose; leave it alone and the accumulated value is sent.
        """
        return self._send(
            MsgType.GRAPH_END,
            n_transitions=self.n_transitions,
            n_output_actions=self.n_output_actions,
            checksum=checksum if checksum is not None else f"{self.rolling:04X}",
        )
