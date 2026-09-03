# SPDX-License-Identifier: LGPL-3.0-or-later
"""A graph set, sent message by message, with the rolling checksum kept.

Was tests/hardware/hardware_test_harness.py, which said of it: *"Both are things the bridge
does, not things a bench instrument does… they stay in the tests"* -- and
daemon/README.md, that anything which starts to look like it is running an
experiment belongs in the bridge. The bridge is this package now, so it does.
The hardware suite imports it from here, which makes that suite a test of the
daemon's codec against real hardware rather than of a copy of it.

A **set**, not a graph: a session uploads every graph it will use before its
first trial and then switches between them with `configure`'s `graph_index`.
See dev/PROTOCOL.md §3.2 and dev/DAEMON.md §3.2.

The checksum is the point. `set_end` carries a CRC-16 over the covered bytes of
every upload message before it (PROTOCOL.md §3.2), so a chunk that went missing
is caught even though the line that vanished was perfectly well formed -- and
computing it from the bytes this side actually sent is the only way to tell that
the device folded the same ones.
"""

from __future__ import annotations

from .message_vocabulary import MsgType
from .request_response_session import RequestResponseSession
from .message_framing import CRC_INIT, command_line, covered_bytes, crc16_ccitt


def send_compiled_upload_messages(
    session: RequestResponseSession,
    upload_messages: list,
    timeout: float = 5.0,
) -> dict:
    """Put a compiled set on the wire and return the `set_ok`.

    `upload_messages` is what `statemachined.graph_set_compiler` produced: an ordered list
    of `(msg_type, fields)` from `set_begin` to `set_end`, with `set_end`'s
    `checksum` left out because it is over bytes that did not exist yet.

    This is the seam. The compiler knows what a name means and nothing about
    framing; this knows the framing and nothing about names. The checksum is the
    reason the two have to meet somewhere: it is folded over the CRC-covered
    bytes of every message actually sent, so it can only be computed by whoever
    sent them.
    """
    rolling_checksum = CRC_INIT
    reply: dict = {}
    for upload_message in upload_messages:
        fields = dict(upload_message.fields)
        is_set_end = upload_message.msg_type == MsgType.SET_END
        if is_set_end:
            fields["checksum"] = f"{rolling_checksum:04X}"

        message_id = session._next_message_id()
        line = command_line(upload_message.msg_type, message_id, **fields)
        if not is_set_end:
            # set_end carries the checksum and so cannot be inside it.
            rolling_checksum = crc16_ccitt(covered_bytes(line), rolling_checksum)
        reply = session.request_line(
            line, message_id, timeout=timeout, what=str(upload_message.msg_type)
        )
    return reply


class GraphSetUploader:
    """One `set_begin` … `set_end`, against one greeted device.

    Counts are counted rather than passed in, because a caller that has to state
    its own totals is a caller that will one day state them wrongly and blame
    the firmware.
    """

    def __init__(self, session: RequestResponseSession, version: int = 1, n_graphs: int = 1,
                 timeout: float = 5.0):
        self.session = session
        self.version = version
        self.n_graphs = n_graphs
        self.timeout = timeout
        self.rolling = CRC_INIT
        #: Set totals, across every graph.
        self.n_states = 0
        self.n_transitions = 0
        self.n_output_actions = 0
        #: This graph's own, which is what `graph_end` carries.
        self._graph_transitions = 0
        self._graph_actions = 0
        self._slot = 0

    def _send(self, msg_type: MsgType, **fields) -> dict:
        message_id = self.session._next_message_id()
        line = command_line(msg_type, message_id, **fields)
        self.rolling = crc16_ccitt(covered_bytes(line), self.rolling)
        return self.session.request_line(
            line, message_id, timeout=self.timeout, what=str(msg_type)
        )

    def begin(self, **fields) -> dict:
        return self._send(
            MsgType.SET_BEGIN, set_version=self.version, n_graphs=self.n_graphs, **fields
        )

    def dist(self, i: int, kind: str = "fixed", **fields) -> dict:
        """One entry of the pool the whole set shares.

        Send these before any state that names one -- either all of them at set
        level or with the graph that introduces them.
        """
        return self._send(MsgType.GRAPH_DIST, i=i, kind=kind, **fields)

    def graph(self, n_states: int, entry: int = 0, slot: int | None = None, **fields) -> dict:
        """Open one graph. `slot` is what `configure`'s `graph_index` will name."""
        self._graph_transitions = 0
        self._graph_actions = 0
        if slot is None:
            slot = self._slot
        self._slot = slot
        return self._send(
            MsgType.GRAPH_BEGIN, slot=slot, n_states=n_states, entry=entry, **fields
        )

    def state(self, i: int, terminal: int | None = None, timeout: dict | None = None) -> dict:
        """One state of the open graph. `i` counts from zero **within it**.

        Both members always present, `terminal` as null where the state is not
        terminal. The protocol distinguishes absent from null and the device
        refuses the former, rather than guessing which a graph meant.
        """
        self.n_states += 1
        return self._send(MsgType.GRAPH_STATE, i=i, terminal=terminal, timeout=timeout)

    def transition(self, target: int, **fields) -> dict:
        """`target` is a state of this graph, counted from zero."""
        self.n_transitions += 1
        self._graph_transitions += 1
        return self._send(MsgType.GRAPH_TRANSITION, target=target, **fields)

    def action(self, on: str, line: int, kind: str = "high", **fields) -> dict:
        self.n_output_actions += 1
        self._graph_actions += 1
        return self._send(MsgType.GRAPH_ACTION, on=on, line=line, kind=kind, **fields)

    def end_graph(self) -> dict:
        """Close the open graph with its own totals, and move the slot on."""
        reply = self._send(
            MsgType.GRAPH_END,
            n_transitions=self._graph_transitions,
            n_output_actions=self._graph_actions,
        )
        self._slot += 1
        return reply

    def end(self, checksum: str | None = None) -> dict:
        """Close the set. The device commits only if its fold agrees.

        `checksum` is an override for the tests that send a wrong one on
        purpose; leave it alone and the accumulated value is sent.
        """
        return self._send(
            MsgType.SET_END,
            n_states=self.n_states,
            n_transitions=self.n_transitions,
            n_output_actions=self.n_output_actions,
            checksum=checksum if checksum is not None else f"{self.rolling:04X}",
        )


class SingleGraphSetUploader:
    """A set of exactly one graph.

    What a bench session, a demo and most of the hardware suite want: they have
    a single paradigm and no interest in slots. It is a wrapper rather than a
    separate path, so the bytes it puts on the wire are the same ones a
    multi-graph session's uploader produces.
    """

    def __init__(self, session: RequestResponseSession, version: int = 1, timeout: float = 5.0):
        self.set = GraphSetUploader(session, version=version, n_graphs=1, timeout=timeout)

    @property
    def version(self) -> int:
        return self.set.version

    @property
    def rolling(self) -> int:
        return self.set.rolling

    def begin(self, n_states: int, entry: int = 0, **fields) -> dict:
        self.set.begin()
        return self.set.graph(n_states, entry=entry, slot=0, **fields)

    def dist(self, i: int, kind: str = "fixed", **fields) -> dict:
        return self.set.dist(i, kind=kind, **fields)

    def state(self, i: int, terminal: int | None = None, timeout: dict | None = None) -> dict:
        return self.set.state(i, terminal=terminal, timeout=timeout)

    def transition(self, target: int, **fields) -> dict:
        return self.set.transition(target, **fields)

    def action(self, on: str, line: int, kind: str = "high", **fields) -> dict:
        return self.set.action(on, line, kind=kind, **fields)

    def end(self, checksum: str | None = None) -> dict:
        """Close the graph and the set. Answered with `set_ok`."""
        self.set.end_graph()
        return self.set.end(checksum)
