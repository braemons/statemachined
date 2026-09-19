# SPDX-License-Identifier: LGPL-3.0-or-later
"""Reading and changing what the box is. docs/reference/api.md §8.

The rig config only: the device, the directories, where triald is. **Not** the
line map or the graphs -- those are a state-machine config
(`state_machine_config_routes.py`), they live under `/var/lib`, and they are
what the web UI writes. This file is backed by a package conffile in
`/etc/braemons` that the daemon never writes back, so a change made here lasts
until the daemon restarts and then the file wins. That is the honest behaviour
for a conffile, and the UI says so rather than implying a change is permanent.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..rig_configuration import RigConfiguration
from .http_errors import refusal
from .rig_service import RigService

router = APIRouter(prefix="/api/config", tags=["config"])


def service_of(request: Request) -> RigService:
    return request.app.state.rig_service


@router.get("")
def read_configuration(request: Request) -> RigConfiguration:
    return service_of(request).configuration


@router.patch("")
def replace_configuration(request: Request, configuration: RigConfiguration) -> dict:
    """Change it, and do whatever the change implies.

    Refused while a trial is armed or running: a target URL that changed means a
    different device, and swapping the device under a running trial would move
    the thing the trial is being measured against.

    **Until the daemon restarts.** This does not write `/etc/braemons`, on
    purpose -- see the note at the top of this file.
    """
    service = service_of(request)
    state_report = service.read_device_state()
    if state_report.get("running") or state_report.get("link_state") == 2:
        raise refusal(409, "busy", "a trial is armed or running", "config")

    device_target_changed = configuration.device_target != service.configuration.device_target
    expected_board_changed = configuration.expected_board != service.configuration.expected_board

    service.configuration = configuration
    service.supervisor.target = configuration.device_target
    service.supervisor.expected_board = configuration.expected_board

    # A board that is no longer the expected one has to be found out about now
    # rather than at the next reconnect, so a changed expectation re-greets the
    # device and is refused there if it does not hold.
    reconnected = False
    if (device_target_changed or expected_board_changed) and service.supervisor.is_connected:
        service.connect()
        reconnected = True

    return {"reconnected": reconnected, "until_restart": True}
