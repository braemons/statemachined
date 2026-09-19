# SPDX-License-Identifier: LGPL-3.0-or-later
"""What the rig is doing, and everything it said.

The trace is the interesting half. An entry is a `dict` with a `kind` and
whatever that kind carries, and **the set of kinds grows with the firmware** —
so the payload crosses as a `google.protobuf.Struct` rather than as a message
per kind. Enumerating them here would mean this daemon could not carry an entry
from a board newer than itself, which is the one thing a trace must always be
able to do.

Four keys are named on the message because every entry has them and a reader
sorts, joins and filters by them. The rest is payload, whatever it is.
"""

from __future__ import annotations

from typing import Any

# protobuf ships no stubs for its own well-known types that a checker can
# follow, so `Struct` reads as unknown. It is exercised by the seam tests,
# which is the check that runs the code.
from google.protobuf import struct_pb2

from statemachined._proto.statemachined.v1 import device_pb2, state_pb2

#: The keys that become fields. Everything else in an entry is payload, so
#: adding a field to the message means taking a key out of the Struct — which
#: is why this list is here rather than repeated at each `pop`.
_NAMED_KEYS = ("entry_number", "kind", "recorded_host_time", "trial_id")


def _struct(value: dict[str, Any]):  # -> struct_pb2.Struct
    """A dict as a `Struct`, dropping what protobuf cannot carry.

    A `Struct` holds null, number, string, bool, list and dict, and nothing
    else. A trace entry is JSON off a serial link so it is already within that
    — but a sink that appended a `datetime` would otherwise raise *inside* the
    stream, thousands of entries in, which is the worst possible place. What
    it cannot carry becomes its `repr`, so the entry still arrives and the
    oddity is visible rather than fatal.
    """
    struct = struct_pb2.Struct()  # ty: ignore[unresolved-attribute]
    for key, item in value.items():
        try:
            struct[key] = item
        except (ValueError, TypeError):
            struct[key] = repr(item)
    return struct


def trace_entry_to_wire(entry: dict[str, Any]) -> state_pb2.TraceEntry:
    message = state_pb2.TraceEntry(
        entry_number=int(entry.get("entry_number", 0)),
        kind=str(entry.get("kind", "")),
        recorded_host_time=str(entry.get("recorded_host_time", "")),
    )
    trial_id = entry.get("trial_id")
    if trial_id is not None:
        message.trial_id = int(trial_id)
    payload = {key: item for key, item in entry.items() if key not in _NAMED_KEYS}
    if payload:
        message.payload.CopyFrom(_struct(payload))
    return message


def trace_window_to_wire(
    entries: list[dict[str, Any]],
    *,
    newest_entry_number: int,
    oldest_entry_number_still_held: int,
    ring_capacity: int,
    lost_entries_before: int | None,
) -> state_pb2.TraceWindow:
    """A window of the ring, and what fell out of it.

    `lost_entries_before` is the only place the ring's boundedness is visible
    from outside. Absent means nothing was lost; saying nothing at all would
    hand back a shorter answer that looks complete, which is how a gap in a
    record becomes a gap in an analysis nobody accounts for.
    """
    window = state_pb2.TraceWindow(
        entries=[trace_entry_to_wire(entry) for entry in entries],
        newest_entry_number=newest_entry_number,
        oldest_entry_number_still_held=oldest_entry_number_still_held,
        ring_capacity=ring_capacity,
    )
    if lost_entries_before is not None:
        window.lost_entries_before = lost_entries_before
    return window


def rig_state_to_wire(
    *,
    connected: bool,
    state_report: dict | None,
    armed_graph_name: str | None,
    state_name: str | None,
    newest_trace_entry_number: int,
) -> state_pb2.RigState:
    """One reading, resolved against the graph this daemon holds.

    `state_name` is passed in rather than looked up here: resolving an index
    needs the committed set, which is the service's to hold. The seam's job is
    to say that a resolved name and an unresolved one are different answers —
    absent, not `""`, because there is no state called nothing.
    """
    report = state_report or {}
    message = state_pb2.RigState(
        connected=connected,
        link_state=int(report.get("link_state", 0) or 0),
        running=bool(report.get("running")),
        graph=armed_graph_name or "",
        newest_trace_entry_number=newest_trace_entry_number,
        scan=_scan(report.get("scan")),
    )
    # Every one of these is `optional` because zero is a real answer for all
    # four: trial 0, state 0, and an input word of 0 with nothing pressed.
    if report.get("trial_id") is not None:
        message.trial_id = int(report["trial_id"])
    if state_name is not None:
        message.state_name = state_name
    if report.get("current_state") is not None:
        message.state_index = int(report["current_state"])
    io = report.get("io") or {}
    if io.get("in") is not None:
        message.input_word = int(io["in"])
    if io.get("out") is not None:
        message.output_word = int(io["out"])
    return message


def _scan(scan: dict | None) -> device_pb2.ScanHealth:
    # The same reading `Device/ReadDevice` reports, from the same key of the
    # same state report. One conversion, so the two answers cannot differ.
    from .device import scan_health_to_wire

    return scan_health_to_wire(scan)


def state_frame_to_wire(sequence: int, state: state_pb2.RigState) -> state_pb2.StateFrame:
    frame = state_pb2.StateFrame(sequence=sequence)
    frame.state.CopyFrom(state)
    return frame


def observer_to_wire(observer: dict[str, Any]) -> state_pb2.Observer:
    return state_pb2.Observer(
        observer_id=str(observer.get("observer_id", "")),
        name=str(observer.get("name") or ""),
        stream=str(observer.get("stream", "")),
        address=str(observer.get("address", "")),
        connected_at_unix_seconds=float(observer.get("connected_at", 0.0)),
        connected_seconds=float(observer.get("connected_seconds", 0.0)),
        delivered=int(observer.get("delivered", 0)),
        fell_behind=bool(observer.get("fell_behind")),
    )


def observers_to_wire(observers: list[dict[str, Any]]) -> state_pb2.Observers:
    return state_pb2.Observers(
        observers=[observer_to_wire(each) for each in observers],
        count=len(observers),
    )
