# SPDX-License-Identifier: LGPL-3.0-or-later
"""What board is attached, what it can hold, and which pin is the left lever.

dev/API.md §3. The half of this API that exists for a person rather than for
triald -- and the reason the daemon is a daemon rather than a library: a
translator can be a library, a thing you can ask at two in the morning whether
the valve is wired to A0 cannot.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from ..model.line_map import LineMap
from .http_errors import no_device_connected, refusal
from .rig_service import RigService

router = APIRouter(prefix="/api/device", tags=["device"])

#: How often the monitor's stream looks for new lines. The link's own reader
#: works in 50 ms bursts, so anything faster would be looking at nothing.
MONITOR_POLL_SECONDS = 0.05


def service_of(request: Request) -> RigService:
    return request.app.state.rig_service


@router.get("")
def read_device(request: Request) -> dict:
    """Everything about the attachment, in one call."""
    service = service_of(request)
    supervisor = service.supervisor
    hello_ack = supervisor.hello_ack or {}
    state_report = service.read_device_state()
    committed = supervisor.committed_graph_set

    return {
        "connected": supervisor.is_connected,
        "target": service.configuration.device_target,
        "board": hello_ack.get("board"),
        "firmware_version": hello_ack.get("fw"),
        "protocol_version": hello_ack.get("proto"),
        "measured_scan_hz": hello_ack.get("scan_hz"),
        # Read from the board, never assumed: the reference board ships two
        # images and a Teensy is a different set of numbers again.
        "capabilities": (
            supervisor.capabilities.model_dump() if supervisor.capabilities else None
        ),
        "has_wiring": state_report.get("has_wiring", hello_ack.get("has_wiring")),
        # Whether this daemon knows which pin each line is because the board
        # said so, or because it assumed. dev/PROTOCOL.md §3.6, and the whole
        # reason that command exists: before it, there was no third answer to
        # "which pin is line 4" beyond a table copied out of the firmware.
        "pin_labels_came_from": supervisor.pin_map.source,
        "committed_set": (
            {
                "set_version": committed.set_version,
                "graph_names": [graph.name for graph in committed.graphs_by_slot],
                "pool_usage": committed.pool_usage,
                "pool_capacity": committed.pool_capacity,
            }
            if committed is not None
            else None
        ),
        "link": {
            "connection_count": supervisor.connection_count,
            "dropped_lines": state_report.get("dropped_lines"),
            "bad_lines": state_report.get("bad_lines"),
            "last_error": service.last_error_from_the_device,
        },
        # Diagnosis rather than control, and the honest half of the timing
        # claim: a board that quietly misses scans looks exactly like one that
        # is fine, so a missed scan is counted and reported.
        "scan": state_report.get("scan"),
        "uptime_device_microseconds": state_report.get("up_us"),
    }


@router.post("/connect")
def connect_to_the_device(request: Request) -> dict:
    """Open the port, greet, and push this rig's wiring. Idempotent."""
    service = service_of(request)
    try:
        return service.connect()
    except Exception as exc:  # noqa: BLE001
        raise no_device_connected(f"could not open {service.configuration.device_target}: {exc}")


@router.get("/lines")
def read_lines(request: Request) -> dict:
    """Every line by name, with what the wiring does to it and its level now.

    `is_high_now` comes from `state_report`'s `io`, which is the only way
    anything outside the device can check that a graph's line numbers reach the
    pins somebody wired: there is no read-back path from a pin.
    """
    service = service_of(request)
    supervisor = service.supervisor
    state_report = service.read_device_state()
    input_word = int(state_report.get("io", {}).get("in", 0))
    output_word = int(state_report.get("io", {}).get("out", 0))
    # Resolved where there is a board to resolve against, so that a line
    # configured by pin reports the index it will actually be uploaded with.
    # Both come from the loaded state-machine config; `line_map` is what it
    # says and `resolved_line_map` is that with the board's answer folded in.
    line_map = supervisor.resolved_line_map if supervisor.is_connected else supervisor.line_map
    pin_map = supervisor.pin_map

    def described(definition, word: int) -> dict:
        line_index = definition.line_index
        # `is_high_now` is the only member added to a line here, and that is a
        # contract rather than an oversight: a client edits what it read and
        # PATCHes it back, and LineMap forbids members it does not declare. The
        # board's own pin names go beside the lists, not inside them.
        return definition.model_dump() | {
            "is_high_now": (bool(word >> line_index & 1) if line_index is not None else None)
        }

    return {
        "input_lines": [described(d, input_word) for d in line_map.input_lines],
        "output_lines": [described(d, output_word) for d in line_map.output_lines],
        # Indexed by line number, so a client reads the board's own name for
        # line 4 as `board_input_pins[4]`.
        #
        # `device` means the board answered `pins` and these labels are its own.
        # `assumed` means this daemon fell back to its own table for firmware
        # older than that command, and every label here is a belief rather than
        # a fact. See dev/PROTOCOL.md §3.6.
        "pin_labels_came_from": pin_map.source,
        "board_input_pins": pin_map.input_pin_labels,
        "board_output_pins": pin_map.output_pin_labels,
    }


@router.patch("/lines")
def replace_the_line_map(request: Request, line_map: LineMap) -> dict:
    """Rename lines, and change the wiring.

    Renaming is free -- names are the daemon's alone and never reach the wire --
    and the rest is pushed to the device in the same call, because a debounce
    that only this side knows about is a debounce that is wrong after a reset.

    **Where this lands, and where it does not.** The map goes to the device and
    into the loaded state-machine config *in memory*. It is not written to
    `/var/lib/braemons/statemachined/configs/` until somebody saves the config
    (`PUT /api/state-machine-configs/{name}`), and this reply says so in
    `saved_to_the_store`. That is the honest shape for a panel somebody is
    editing while watching a lamp: a wiring change has to reach the board
    immediately to be *checked* against the wire, and an edit that reached the
    disk on every keystroke would make "revert" mean nothing.

    A rig with no config loaded takes the map anyway and holds it in the
    supervisor. It has nowhere to save it -- there is no config to put it in --
    which the reply also says.
    """
    service = service_of(request)
    supervisor = service.supervisor
    if supervisor.is_connected:
        # Resolved against the board *before* anything is kept, so a map naming
        # a pin this board does not have is refused with the rig still running
        # on the map it had. See dev/PROTOCOL.md §3.6.
        try:
            resolved = line_map.resolved_against(supervisor.pin_map)
        except ValueError as exc:
            raise refusal(422, "line_map_does_not_match_the_board", str(exc), "line_map")
    else:
        resolved = line_map

    supervisor.line_map = line_map
    supervisor.resolved_line_map = resolved
    config = service.state_machine_config
    if config is not None:
        config.line_map = line_map
    if supervisor.is_connected:
        with service.device_lock:
            supervisor.push_wiring()

    return {
        "pushed_to_device": supervisor.is_connected,
        "line_map": line_map.model_dump(),
        "resolved_line_map": resolved.model_dump(),
        # Never true here, and named rather than omitted: a UI has to be able to
        # tell a person their edit is one restart away from being lost.
        "saved_to_the_store": False,
        "state_machine_config": config.name if config is not None else None,
    }


# ------------------------------------------------------- the serial monitor ---


@router.get("/monitor")
def read_the_line_monitor(request: Request, since_entry_number: int = 0, limit: int = 500) -> dict:
    """The last lines in and out of the port, in the protocol's own words.

    dev/API.md §3. What this is for is the moment the layers stop agreeing: the
    line map says the valve is line 3, the valve is not opening, and the
    question is what actually went down the wire. Nothing here interprets
    anything -- these are the lines, in order, with the time they crossed.

    Bounded, and a caller that has fallen behind is **told what it missed**
    rather than handed a shorter answer that looks complete.
    """
    service = service_of(request)
    monitor = service.line_monitor
    oldest_still_held = monitor.oldest_entry_number_still_held()
    return {
        "lines": monitor.lines_since(since_entry_number, limit=limit),
        "newest_entry_number": monitor.newest_entry_number(),
        "oldest_entry_number_still_held": oldest_still_held,
        "ring_capacity": monitor.ring_capacity,
        "lost_lines_before": (
            oldest_still_held if monitor.has_fallen_out_of_the_ring(since_entry_number) else None
        ),
    }


@router.websocket("/monitor/stream")
async def stream_the_line_monitor(websocket: WebSocket) -> None:
    """Every line as it crosses, in order, none skipped.

    Not coalesced, for the same reason the trace's stream is not: a monitor that
    dropped frames to keep up would be lying about the one thing it exists to
    show. A client too slow for the ring is told the range it lost and the
    socket closes -- one that believes it saw everything is worse than one that
    knows it did not.
    """
    await websocket.accept()
    service: RigService = websocket.app.state.rig_service
    monitor = service.line_monitor
    next_entry_number = monitor.newest_entry_number() + 1
    try:
        while True:
            if monitor.has_fallen_out_of_the_ring(next_entry_number):
                oldest = monitor.oldest_entry_number_still_held()
                await websocket.send_json(
                    {
                        "error": "fell_out_of_the_ring",
                        "lost_from_entry_number": next_entry_number,
                        "lost_to_entry_number": oldest - 1,
                    }
                )
                await websocket.close(code=1011)
                return
            for line in monitor.lines_since(next_entry_number, limit=500):
                await websocket.send_json(line)
                next_entry_number = line["entry_number"] + 1
            await asyncio.sleep(MONITOR_POLL_SECONDS)
    except (WebSocketDisconnect, RuntimeError):
        return


@router.get("/firmware")
def read_firmware_versions(request: Request) -> dict:
    """The version running against what the installed package ships.

    The comparison is what matters: a board in a rack cannot be asked which
    commit it is running, so the package carries a manifest and this says
    whether the two agree. Flashing is deliberately not here -- it means
    dropping the port mid-session, which is a different risk from anything else
    the daemon does. See dev/DAEMON.md §6.3.
    """
    service = service_of(request)
    running = (service.supervisor.hello_ack or {}).get("fw")
    installed = _installed_firmware_version()
    if running is None:
        raise refusal(503, "not_connected", "no device is connected", "device")
    return {
        "running": running,
        "installed": installed,
        "matches": installed is not None and running == installed,
    }


def _installed_firmware_version() -> str | None:
    """From the package's MANIFEST.txt, when there is one.

    None on a checkout, which is not an error: `make image` writes the manifest
    and the package installs it, so its absence means "nobody installed a
    firmware image here", which is the truth on a developer's machine.
    """
    from pathlib import Path

    manifest = Path("/usr/share/braemons/statemachined/firmware/MANIFEST.txt")
    if not manifest.exists():
        return None
    for line in manifest.read_text().splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None
