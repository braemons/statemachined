# SPDX-License-Identifier: LGPL-3.0-or-later
"""The saved configs: what this rig is wired like, and what it can run.

dev/API.md §8. The half of the configuration a person owns -- the line map and
the graphs -- as opposed to the box, which is `/api/config` and a file in
`/etc/braemons` this daemon never writes.

**Saving and loading are different verbs and this router keeps them apart.**
`PUT` writes a config to the store and changes nothing about the running rig.
`POST /{name}/load` makes one the rig's: the map is resolved against the board,
refused if it does not match, and pushed. A UI that could only save by also
arming the rig is a UI nobody edits during a session.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..model.state_machine_config import StateMachineConfig
from ..state_machine_config_store import ConfigNameMismatch, ConfigNotInStore
from .http_errors import refusal
from .rig_service import RigService

router = APIRouter(prefix="/api/state-machine-configs", tags=["state-machine-config"])


def service_of(request: Request) -> RigService:
    return request.app.state.rig_service


@router.get("")
def list_stored_configs(request: Request) -> dict:
    """Every saved config, with enough of each to choose between them.

    Summaries rather than whole configs: a config carries its graphs, so ten of
    them is most of a megabyte nobody is looking at yet. A config that will not
    parse is listed with the reason instead of omitted -- a file you cannot see
    is a file you cannot fix.
    """
    service = service_of(request)
    return {
        "configs": service.state_machine_config_store.summaries(),
        "loaded": (
            service.state_machine_config.name
            if service.state_machine_config is not None
            else None
        ),
    }


@router.get("/{config_name}")
def read_stored_config(request: Request, config_name: str) -> StateMachineConfig:
    try:
        return service_of(request).state_machine_config_store.load(config_name)
    except ConfigNotInStore as exc:
        raise refusal(404, "no_such_state_machine_config", str(exc), "config_name")
    except (ConfigNameMismatch, ValueError) as exc:
        raise refusal(422, "unreadable_state_machine_config", str(exc), "config_name")


@router.put("/{config_name}")
def write_stored_config(
    request: Request, config_name: str, config: StateMachineConfig
) -> dict:
    """Save one. Does not load it, and does not touch the device.

    The name in the path and the name in the body must agree, because a config
    stored under a name that is not its own is one the store refuses to load
    back -- and it would be discovered on the morning somebody needed it.
    """
    if config.name != config_name:
        raise refusal(
            422,
            "name_mismatch",
            f"the body is a config called {config.name!r}, stored as {config_name!r}",
            "name",
        )
    service = service_of(request)
    service.save_state_machine_config(config)
    return {
        "name": config.name,
        "graph_names": [graph.name for graph in config.graphs],
        # Whether the rig is now running what was just written. Saving over the
        # loaded config does not re-apply it -- the answer is "no" until
        # somebody loads it -- and a UI that did not say so would leave a person
        # believing a wiring change had reached the board.
        "is_the_loaded_config": (
            service.state_machine_config is not None
            and service.state_machine_config.name == config.name
        ),
    }


@router.delete("/{config_name}")
def delete_stored_config(request: Request, config_name: str) -> dict:
    """Remove one from the store.

    A loaded config that is deleted stays loaded: the rig goes on running what
    it was given, because deleting a file is not a request to stop an
    experiment. `GET /api/session` says the config is no longer in the store.
    """
    service = service_of(request)
    try:
        service.state_machine_config_store.delete(config_name)
    except ConfigNotInStore as exc:
        raise refusal(404, "no_such_state_machine_config", str(exc), "config_name")
    return {"deleted": config_name}


@router.post("/{config_name}/load")
def load_stored_config(request: Request, config_name: str) -> dict:
    """Make one the rig's: apply its line map, and push the wiring.

    Refused while a trial is armed or running, like every other change to the
    wiring: a line map is what the trial's own record means, and moving it
    mid-trial makes that record a fiction.

    The graphs are not uploaded here -- `POST /api/session/open` does that. See
    `RigService.apply_state_machine_config`.
    """
    service = service_of(request)
    state_report = service.read_device_state()
    if state_report.get("running") or state_report.get("link_state") == 2:
        raise refusal(409, "busy", "a trial is armed or running", "config_name")

    try:
        config = service.load_state_machine_config(config_name)
    except ConfigNotInStore as exc:
        raise refusal(404, "no_such_state_machine_config", str(exc), "config_name")
    except ValueError as exc:
        # Both a config that will not parse and a line map this board refuses.
        # The message names which, and it is the message a person acts on.
        raise refusal(422, "state_machine_config_does_not_match_the_board", str(exc), "line_map")
    return {
        "loaded": config.name,
        "wiring_pushed": service.supervisor.is_connected,
        "line_map": service.supervisor.resolved_line_map.model_dump(),
        "graph_names": [graph.name for graph in config.graphs],
    }
