# SPDX-License-Identifier: LGPL-3.0-or-later
"""What board is attached, what it can hold, and which pin is the left lever.

dev/API.md §3. The half of this API that exists for a person rather than for
triald -- and the reason the daemon is a daemon rather than a library: a
translator can be a library, a thing you can ask at two in the morning whether
the valve is wired to A0 cannot.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..model.line_map import LineMap
from .http_errors import no_device_connected, refusal
from .rig_service import RigService

router = APIRouter(prefix="/api/device", tags=["device"])


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
    state_report = service.read_device_state()
    input_word = int(state_report.get("io", {}).get("in", 0))
    output_word = int(state_report.get("io", {}).get("out", 0))
    line_map = service.configuration.line_map

    return {
        "input_lines": [
            definition.model_dump() | {"is_high_now": bool(input_word >> definition.line_index & 1)}
            for definition in line_map.input_lines
        ],
        "output_lines": [
            definition.model_dump()
            | {"is_high_now": bool(output_word >> definition.line_index & 1)}
            for definition in line_map.output_lines
        ],
    }


@router.patch("/lines")
def replace_the_line_map(request: Request, line_map: LineMap) -> dict:
    """Rename lines, and change the wiring.

    Renaming is free -- names are the daemon's alone and never reach the wire --
    and the rest is pushed to the device in the same call, because a debounce
    that only this side knows about is a debounce that is wrong after a reset.
    """
    service = service_of(request)
    service.configuration.line_map = line_map
    service.supervisor.line_map = line_map
    if not service.supervisor.is_connected:
        return {"pushed_to_device": False, "line_map": line_map.model_dump()}
    with service.device_lock:
        service.supervisor.push_wiring()
    return {"pushed_to_device": True, "line_map": line_map.model_dump()}


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
