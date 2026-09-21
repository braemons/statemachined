# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recordings, and the gaps in them.

The one shape worth reading twice is `segments`. **A recording with a pause in
it has no contiguous range of entry numbers**, because the numbers are the
trace's and the gap is real. The segments are where that gap is written down;
without them a reader sees a jump and has to guess whether it was a pause or a
loss, and those are very different findings.
"""

from __future__ import annotations

from typing import Any

from statemachined._proto.statemachined.v1 import recording_pb2

from .state import trace_entry_to_wire


def segment_to_wire(segment: dict[str, Any]) -> recording_pb2.RecordingSegment:
    """One stretch that was actually being written.

    `from_entry_number` is absent on a segment that was opened and never
    written to — a recording started and paused before anything happened —
    and zero is a real entry number, so the two have to stay distinguishable.
    """
    message = recording_pb2.RecordingSegment(
        started_host_time=str(segment.get("started_host_time") or ""),
        ended_host_time=str(segment.get("ended_host_time") or ""),
        entry_count=int(segment.get("entry_count", 0)),
    )
    if segment.get("from_entry_number") is not None:
        message.from_entry_number = int(segment["from_entry_number"])
    if segment.get("to_entry_number") is not None:
        message.to_entry_number = int(segment["to_entry_number"])
    return message


def manifest_to_wire(manifest: dict[str, Any]) -> recording_pb2.RecordingManifest:
    """What a recording is, without its entries.

    A manifest that will not parse arrives here as `{name, unreadable}` and
    nothing else, and crosses as itself: listed with the reason rather than
    omitted, because a file you cannot see is a file you cannot fix.
    """
    return recording_pb2.RecordingManifest(
        name=str(manifest.get("name", "")),
        description=str(manifest.get("description", "")),
        state_machine_config=str(manifest.get("state_machine_config") or ""),
        created_unix_seconds=float(manifest.get("created_unix_seconds") or 0.0),
        created_host_time=str(manifest.get("created_host_time") or ""),
        state=str(manifest.get("state", "")),
        segments=[segment_to_wire(each) for each in manifest.get("segments", [])],
        entry_count=int(manifest.get("entry_count", 0)),
        kind_counts={
            str(kind): int(count) for kind, count in (manifest.get("kind_counts") or {}).items()
        },
        unreadable=str(manifest.get("unreadable") or ""),
    )


def recordings_to_wire(
    manifests: list[dict[str, Any]], *, active: dict[str, Any] | None
) -> recording_pb2.Recordings:
    message = recording_pb2.Recordings(
        recordings=[manifest_to_wire(each) for each in manifests]
    )
    if active is not None:
        message.active.CopyFrom(manifest_to_wire(active))
    return message


def recording_entries_to_wire(
    name: str,
    entries: list[dict[str, Any]],
    *,
    offset: int,
    entry_count: int,
    segments: list[dict[str, Any]],
) -> recording_pb2.RecordingEntries:
    """A page of a recording, **by position in the file**.

    By position rather than by entry number, because of the gap above. Each
    entry still carries its own `entry_number`, so a reader can join it back to
    the trace exactly.
    """
    return recording_pb2.RecordingEntries(
        name=name,
        offset=offset,
        entries=[trace_entry_to_wire(entry) for entry in entries],
        entry_count=entry_count,
        segments=[segment_to_wire(each) for each in segments],
    )
