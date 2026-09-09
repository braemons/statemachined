# SPDX-License-Identifier: LGPL-3.0-or-later
"""Recording the trace into a named file. dev/API.md §9.

Four verbs and a store. What is being recorded is the trace, which is running
either way -- these calls choose what is **kept under a name**, and a pause
therefore leaves a gap that the manifest states rather than a blindness that
nothing records. See `event_recording.py` for why that distinction is the whole
design.

Everything here is a call triald could make and a button the web UI has. That is
the point of the pair existing: a bench with nobody driving must be able to do
what a rig with triald does, or the bench proves nothing about the rig.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ..event_recording import (
    BadRecordingName,
    RecordingNameTaken,
    RecordingNotInStore,
    RecordingStateRefused,
)
from .http_errors import refusal
from .rig_service import RigService

router = APIRouter(prefix="/api/recordings", tags=["recording"])


def service_of(request: Request) -> RigService:
    return request.app.state.rig_service


class StartRecordingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Empty means "name it after the time it started", which is what somebody
    #: pressing a button on a bench wants and is still a name that sorts.
    name: str = ""
    description: str = Field(default="", max_length=1000)


@router.get("")
def list_recordings(request: Request) -> dict:
    """Every recording on this rig, and which one is being written."""
    service = service_of(request)
    return {"active": service.recorder.active, "recordings": service.recorder.manifests()}


@router.post("/start")
def start_recording(request: Request, body: StartRecordingRequest | None = None) -> dict:
    service = service_of(request)
    body = body or StartRecordingRequest()
    try:
        return service.start_recording(body.name or None, description=body.description)
    except BadRecordingName as exc:
        raise refusal(422, "bad_recording_name", str(exc), "name")
    except RecordingNameTaken as exc:
        raise refusal(409, "recording_name_taken", str(exc), "name")
    except RecordingStateRefused as exc:
        raise refusal(409, "already_recording", str(exc), "recording")


@router.post("/pause")
def pause_recording(request: Request) -> dict:
    """Stop capturing, keep the recording open. The rig is not blinded by this."""
    try:
        return service_of(request).pause_recording()
    except RecordingStateRefused as exc:
        raise refusal(409, "not_recording", str(exc), "recording")


@router.post("/resume")
def resume_recording(request: Request) -> dict:
    try:
        return service_of(request).resume_recording()
    except RecordingStateRefused as exc:
        raise refusal(409, "not_paused", str(exc), "recording")


@router.post("/stop")
def stop_recording(request: Request) -> dict:
    """End it. The file stays and the recording is in the store."""
    try:
        return service_of(request).stop_recording()
    except RecordingStateRefused as exc:
        raise refusal(409, "not_recording", str(exc), "recording")


@router.post("/clear")
def clear_recording(request: Request) -> dict:
    """Throw away what has been captured so far and keep recording.

    Distinct from stop, which keeps what it has, and from `DELETE
    /api/recordings/{name}`, which removes a recording nobody is writing.
    """
    try:
        return service_of(request).clear_recording()
    except RecordingStateRefused as exc:
        raise refusal(409, "not_recording", str(exc), "recording")


@router.get("/{name}")
def read_recording(request: Request, name: str) -> dict:
    try:
        return service_of(request).recorder.manifest_of(name)
    except BadRecordingName as exc:
        raise refusal(422, "bad_recording_name", str(exc), "name")
    except RecordingNotInStore as exc:
        raise refusal(404, "no_such_recording", str(exc), "name")


@router.get("/{name}/entries")
def read_recording_entries(
    request: Request, name: str, offset: int = 0, limit: int = 500
) -> dict:
    """The entries, by position in the file.

    By position rather than by entry number: a recording with a pause in it has
    no contiguous range of entry numbers, because the numbers are the trace's
    and the gap is real. Each entry still carries its `entry_number`, so a
    reader can join it back to the trace exactly.
    """
    service = service_of(request)
    try:
        entries = service.recorder.entries_of(name, offset=offset, limit=limit)
        manifest = service.recorder.manifest_of(name)
    except BadRecordingName as exc:
        raise refusal(422, "bad_recording_name", str(exc), "name")
    except RecordingNotInStore as exc:
        raise refusal(404, "no_such_recording", str(exc), "name")
    return {
        "name": name,
        "offset": offset,
        "entries": entries,
        "entry_count": manifest.get("entry_count", 0),
        "segments": manifest.get("segments", []),
    }


@router.delete("/{name}")
def delete_recording(request: Request, name: str) -> dict:
    try:
        service_of(request).recorder.delete(name)
    except BadRecordingName as exc:
        raise refusal(422, "bad_recording_name", str(exc), "name")
    except RecordingStateRefused as exc:
        raise refusal(409, "recording_in_progress", str(exc), "name")
    except RecordingNotInStore as exc:
        raise refusal(404, "no_such_recording", str(exc), "name")
    return {"deleted": name}
