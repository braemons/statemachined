# SPDX-License-Identifier: AGPL-3.0-or-later
"""What is happening right now, and everything the device said.

The two streams are here together because they are the same subscription seen
at two rates: `WatchState` is a whole state whenever one changes, and
`WatchTrace` is every entry in order. A console wants the first; triald wants
the second.
"""

from __future__ import annotations

import asyncio
import contextlib

from statemachined._proto.statemachined.v1 import service_pb2_grpc, state_pb2
from statemachined.daemon.api import convert
from statemachined.daemon.api.rig_service import RigService
from statemachined.graph_set_compiler import GraphNotInSet

from .observer_registration import watching
from .refusals import answering, reading

#: How often a watcher looks for a change. Coarse on purpose: this is a console
#: refreshing, not a control loop, and the device's own scan is four orders of
#: magnitude faster than anything a person reads.
WATCH_PERIOD_SECONDS = 0.1


class StateServicer(service_pb2_grpc.StateServicer):
    def __init__(self, service: RigService) -> None:
        self.service = service

    def _state(self) -> state_pb2.RigState:
        """One reading, with the state index resolved to a name where it can be.

        The resolution needs the committed set and the armed graph, which is
        why it happens here rather than in the seam: the seam is handed an
        answer, not the two things it takes to work one out.

        **"Where it can be" is the whole of it, and it must never refuse.**
        `armed_graph_name` is the last graph this daemon armed, and the
        committed set can have been replaced since — a session that uploads a
        new set between trials leaves a name the set no longer has. Reading
        what the rig is doing must survive that: the *name* is a convenience
        over `state_index`, which is always there, and a `ReadState` that
        refused because of it would take the whole panel down over a label.

        Found by the three-daemon suite, which runs two trials on two different
        sets and read a refusal off the second.
        """
        supervisor = self.service.supervisor
        report = self.service.read_device_state()
        committed = supervisor.committed_graph_set
        index = report.get("current_state")
        name = None
        if committed is not None and supervisor.armed_graph_name and index is not None:
            with contextlib.suppress(GraphNotInSet):
                names = committed.graph_named(supervisor.armed_graph_name).state_names_by_index
                if 0 <= index < len(names):
                    name = names[index]
        return convert.rig_state_to_wire(
            connected=supervisor.is_connected,
            state_report=report,
            armed_graph_name=supervisor.armed_graph_name,
            state_name=name,
            newest_trace_entry_number=self.service.trace.newest_entry_number(),
        )

    async def ReadState(self, request, context):
        return await answering(context, lambda: self._state())

    async def WatchState(self, request, context):
        """The current state at once, then one frame per change.

        **At once, and not on the first change**: a client that connected to a
        quiet rig and saw nothing would have no way to tell that from a rig
        that is not there.

        Frames are coalesced by construction — each is a whole state read at
        the moment it is sent — so `sequence` counts what was *sent* and a
        client never has to reconcile a backlog.
        """
        with watching(self.service, context, stream="state") as observer_id:
            sequence = 0
            previous = None
            while True:
                state = await reading(self._state)
                if previous is None or state != previous:
                    yield convert.state_frame_to_wire(sequence, state)
                    sequence += 1
                    previous = state
                    self.service.observers.note_delivery(observer_id)
                await asyncio.sleep(WATCH_PERIOD_SECONDS)

    async def ReadTrace(self, request, context):
        def body():
            trace = self.service.trace
            oldest = trace.oldest_entry_number_still_held()
            return convert.trace_window_to_wire(
                trace.entries_since(request.since_entry_number, limit=request.limit or 500),
                newest_entry_number=trace.newest_entry_number(),
                oldest_entry_number_still_held=oldest,
                ring_capacity=trace.ring_capacity,
                lost_entries_before=(
                    oldest
                    if trace.has_fallen_out_of_the_ring(request.since_entry_number)
                    else None
                ),
            )

        return await answering(context, body)

    async def WatchTrace(self, request, context):
        """Everything from `since_entry_number` onwards, and then as it happens.

        **This is what triald subscribes to**, and what a recording is written
        from. The backlog goes first so that a subscriber which reconnects
        picks up where it left off rather than losing whatever happened while
        it was away — the ring is what makes that possible, and
        `lost_entries_before` on `ReadTrace` is how a client learns the ring
        was not enough.
        """
        trace = self.service.trace
        next_entry_number = request.since_entry_number
        with watching(self.service, context, stream="trace") as observer_id:
            while True:
                if trace.has_fallen_out_of_the_ring(next_entry_number):
                    # The subscriber is behind the ring and entries are gone.
                    # Noted rather than refused: the stream carries on from
                    # whatever is left, and the client sees the gap in the
                    # entry numbers. Marking it here is what makes a fallen
                    # -behind consumer visible to the person looking for it.
                    self.service.observers.note_fell_behind(observer_id)
                    next_entry_number = trace.oldest_entry_number_still_held()
                # Bound explicitly: the lambda runs on another thread, and
                # `next_entry_number` is reassigned in this loop. It happens to
                # be awaited immediately, which is exactly the kind of "happens
                # to" that stops being true when somebody adds a line.
                entries = await reading(
                    lambda since=next_entry_number: trace.entries_since(since, limit=500)
                )
                for entry in entries:
                    yield convert.trace_entry_to_wire(entry)
                    next_entry_number = entry["entry_number"] + 1
                self.service.observers.note_delivery(observer_id, len(entries))
                if not entries:
                    await asyncio.sleep(WATCH_PERIOD_SECONDS)

    async def ReadTrialTrace(self, request, context):
        def body():
            return state_pb2.TrialTrace(
                trial_id=request.trial_id,
                entries=[
                    convert.trace_entry_to_wire(entry)
                    for entry in self.service.trace.entries_for_trial(request.trial_id)
                ],
            )

        return await answering(context, body)

    async def ReadObservers(self, request, context):
        def body():
            return convert.observers_to_wire(
                [each.as_dict() for each in self.service.observers.observers()]
            )

        return await answering(context, body)
