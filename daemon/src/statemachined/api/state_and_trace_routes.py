# SPDX-License-Identifier: LGPL-3.0-or-later
"""What the machine is doing now, and everything it has done.

dev/API.md §6 and §7. Two streams with opposite rules, and the difference is the
only interesting thing here:

  * `WS /api/stream` is **coalesced**. A client that falls behind gets the
    current state rather than a backlog of stale ones, because coalescing a
    snapshot loses nothing -- the latest one is the whole truth. It is triald's
    convention and it exists so a slow browser tab cannot hold up a session.

  * `WS /api/trace/stream` is **not**. Coalescing a trace loses records, which
    is the one thing it exists not to do. A client slow enough to fall out of
    the ring is told so, with the range that is gone, rather than handed a
    shorter answer that looks complete -- buffering per client is how a
    monitoring aid becomes the thing that fills the Pi's memory.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from ..observer_registry import Observer
from .http_errors import refusal
from .rig_service import RigService

router = APIRouter(tags=["state"])

#: How often a stream looks for something new. The device's own timing is
#: nowhere near this -- a scan is 100 us -- and it does not need to be: these
#: are for watching, and dev/PROTOCOL.md is explicit that a monitoring aid never
#: sits in a trial's critical path.
STREAM_POLL_SECONDS = 0.05


def service_of(request: Request) -> RigService:
    return request.app.state.rig_service


def build_state_snapshot(service: RigService) -> dict:
    """The one frame both `GET /api/state` and the stream send."""
    supervisor = service.supervisor
    state_report = service.read_device_state()
    committed = supervisor.committed_graph_set

    state_name = None
    current_state_index = state_report.get("current_state")
    if committed is not None and supervisor.armed_graph_name and current_state_index is not None:
        names = committed.graph_named(supervisor.armed_graph_name).state_names_by_index
        if 0 <= current_state_index < len(names):
            state_name = names[current_state_index]

    return {
        "connected": supervisor.is_connected,
        "link_state": state_report.get("link_state"),
        "running": state_report.get("running"),
        "trial_id": state_report.get("trial_id"),
        "graph": supervisor.armed_graph_name,
        # By name where the daemon can say it: an index means nothing to
        # somebody watching a rig, and the daemon holds the graph.
        "state_name": state_name,
        "state_index": current_state_index,
        "input_word": state_report.get("io", {}).get("in"),
        "output_word": state_report.get("io", {}).get("out"),
        "scan": state_report.get("scan"),
        "newest_trace_entry_number": service.trace.newest_entry_number(),
    }


@router.get("/api/state")
def read_state(request: Request) -> dict:
    return build_state_snapshot(service_of(request))


@router.websocket("/api/stream")
async def stream_state(websocket: WebSocket) -> None:
    """Coalesced frames: the current state, repeatedly, and never a backlog."""
    await websocket.accept()
    service: RigService = websocket.app.state.rig_service
    observer_id = _register_observer(websocket, service, stream="state")
    previous_frame: dict | None = None
    try:
        while True:
            # Read the device under the service's lock in a worker thread: this
            # coroutine must not block the event loop on a serial port.
            frame = await asyncio.to_thread(build_state_snapshot, service)
            if frame != previous_frame:
                await websocket.send_json(frame)
                service.observers.note_delivery(observer_id)
                previous_frame = frame
            await asyncio.sleep(STREAM_POLL_SECONDS)
    except (WebSocketDisconnect, RuntimeError):
        return
    finally:
        service.observers.unregister(observer_id)


# --------------------------------------------------------------- the trace ---

trace_router = APIRouter(prefix="/api/trace", tags=["trace"])


@trace_router.get("")
def read_trace(request: Request, since_entry_number: int = 0, limit: int = 500) -> dict:
    """The ring, oldest first, from a cursor.

    A caller whose cursor has fallen out of the ring is **told so**, with the
    range that is gone. It is the one place the ring's boundedness is visible
    from outside, and saying nothing would hand back a shorter answer that looks
    complete.
    """
    service = service_of(request)
    entries = service.trace.entries_since(since_entry_number, limit=limit)
    oldest_still_held = service.trace.oldest_entry_number_still_held()
    return {
        "entries": entries,
        "newest_entry_number": service.trace.newest_entry_number(),
        "oldest_entry_number_still_held": oldest_still_held,
        "ring_capacity": service.trace.ring_capacity,
        "lost_entries_before": (
            oldest_still_held
            if service.trace.has_fallen_out_of_the_ring(since_entry_number)
            else None
        ),
    }


@trace_router.get("/trial/{trial_id}")
def read_trace_for_one_trial(request: Request, trial_id: int) -> dict:
    """One trial's entries. What triald fetches if it stows the trace beside the .tdr.

    Pull, not push: the daemon never sends this anywhere and never assumes
    anybody read it. If nobody does, the trace still exists on the rig.
    """
    service = service_of(request)
    entries = service.trace.entries_for_trial(trial_id)
    if not entries:
        raise refusal(
            404,
            "no_trace_for_trial",
            f"the ring holds nothing for trial {trial_id}",
            "trial_id",
        )
    return {"trial_id": trial_id, "entries": entries}


@trace_router.websocket("/stream")
async def stream_trace(websocket: WebSocket) -> None:
    """Every entry as it arrives, in order, none skipped.

    **This is how anything observes a rig**, triald included. There is nothing to
    register and nothing to be granted: opening the socket is the whole of
    subscribing, closing it is the whole of unsubscribing, and the daemon does
    not act on who is here. `?observer=<name>` labels the connection for
    `GET /api/observers` and the web UI, so a person can see that triald is
    listening -- which is the first question anybody asks when trials stop being
    recorded. Unnamed is fine.
    """
    await websocket.accept()
    service: RigService = websocket.app.state.rig_service
    observer_id = _register_observer(websocket, service, stream="trace")
    next_entry_number = service.trace.newest_entry_number() + 1
    try:
        while True:
            if service.trace.has_fallen_out_of_the_ring(next_entry_number):
                service.observers.note_fell_behind(observer_id)
                # Genuinely lost data. Say so and close, rather than silently
                # resuming from whatever is left -- a consumer that believes it
                # saw everything is worse than one that knows it did not.
                oldest = service.trace.oldest_entry_number_still_held()
                await websocket.send_json(
                    {
                        "error": "fell_out_of_the_ring",
                        "lost_from_entry_number": next_entry_number,
                        "lost_to_entry_number": oldest - 1,
                    }
                )
                await websocket.close(code=1011)
                return

            entries = service.trace.entries_since(next_entry_number, limit=500)
            for entry in entries:
                await websocket.send_json(entry)
                next_entry_number = entry["entry_number"] + 1
            service.observers.note_delivery(observer_id, len(entries))
            await asyncio.sleep(STREAM_POLL_SECONDS)
    except (WebSocketDisconnect, RuntimeError):
        return
    finally:
        service.observers.unregister(observer_id)


# ----------------------------------------------------------- the observers ---


def _register_observer(websocket: WebSocket, service: RigService, *, stream: str) -> int:
    """Note that this socket is watching, and say what to call it."""
    client = websocket.client
    return service.observers.register(
        name=websocket.query_params.get("observer"),
        stream=stream,
        address=f"{client.host}:{client.port}" if client else None,
    )


@router.get("/api/observers")
def read_observers(request: Request) -> dict:
    """Who is watching this rig right now.

    A diagnostic, not a contract. The daemon does not act on this list, does not
    wait for anybody in it, and does not remember it across a restart -- it
    publishes and assumes nobody read it. What the list is for is the question a
    person asks when trials stop reaching triald, which is otherwise answered
    with a packet capture.
    """
    observers: list[Observer] = service_of(request).observers.observers()
    return {
        "observers": [observer.as_dict() for observer in observers],
        "count": len(observers),
    }
