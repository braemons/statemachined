# SPDX-License-Identifier: LGPL-3.0-or-later
"""Reading and changing what this rig differs by. dev/API.md §8."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..daemon_configuration import DaemonConfiguration
from .http_errors import refusal
from .rig_service import RigService

router = APIRouter(prefix="/api/config", tags=["config"])


def service_of(request: Request) -> RigService:
    return request.app.state.rig_service


@router.get("")
def read_configuration(request: Request) -> DaemonConfiguration:
    return service_of(request).configuration


@router.patch("")
def replace_configuration(request: Request, configuration: DaemonConfiguration) -> dict:
    """Change it, and do whatever the change implies.

    Refused while a trial is armed or running. A target URL that changed means a
    different device, a line map that changed means different wiring, and doing
    either under a running trial would move something the trial is being
    measured against.
    """
    service = service_of(request)
    state_report = service.read_device_state()
    if state_report.get("running") or state_report.get("link_state") == 2:
        raise refusal(409, "busy", "a trial is armed or running", "config")

    device_target_changed = configuration.device_target != service.configuration.device_target
    line_map_changed = configuration.line_map != service.configuration.line_map

    service.configuration = configuration
    service.supervisor.line_map = configuration.line_map
    service.supervisor.target = configuration.device_target

    reconnected = False
    if device_target_changed and service.supervisor.is_connected:
        service.connect()
        reconnected = True
    elif line_map_changed and service.supervisor.is_connected:
        with service.device_lock:
            service.supervisor.push_wiring()

    return {
        "reconnected": reconnected,
        "wiring_pushed": line_map_changed and not reconnected,
    }
