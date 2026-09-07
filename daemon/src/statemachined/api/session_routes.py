# SPDX-License-Identifier: LGPL-3.0-or-later
"""Opening and closing a session. dev/API.md §4.

A session is one run of an experiment: the graphs it will use, put on the device
once, before an animal is in the booth. After that a trial names one of them and
starts in milliseconds, which is the whole reason the upload happens here rather
than per trial (dev/DAEMON.md §3.2).

**On a rig, triald opens the session. On a bench, a person does.** Same call,
which is the point -- a bench that exercised a different path would be a bench
that proves nothing about the rig. What differs is only who pressed it.

The older `POST /api/session/graphs` in `graph_routes.py` is the same upload
over graphs named out of the store, and it stays: it is what triald has always
called, and it does not need a state-machine config to exist. `POST
/api/session/open` is the one to use where there is a config, because then the
line map and the graphs came from one file that was saved together.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from ..device.message_framing import DeviceRefusedTheCommand
from ..graph_set_compiler import GraphSetCompilationError
from .http_errors import from_device_refusal, no_device_connected, refusal
from .rig_service import GraphNotInTheLoadedConfig, NoConfigLoaded, RigService

router = APIRouter(prefix="/api/session", tags=["session"])


def service_of(request: Request) -> RigService:
    return request.app.state.rig_service


@router.get("")
def read_session(request: Request) -> dict:
    """What this rig is loaded with, and whether a session is open.

    Three separate facts, reported separately rather than collapsed into one
    "ready" flag, because the useful question at two in the morning is *which*
    of them is missing: a config may be loaded with no session open, and a set
    may be committed on the board from a session that ended -- which is normal,
    and is what makes a reconnect cheap.
    """
    service = service_of(request)
    config = service.state_machine_config
    committed = service.supervisor.committed_graph_set
    stored_names = service.state_machine_config_store.stored_config_names()
    return {
        "state_machine_config": (
            {
                "name": config.name,
                "description": config.description,
                "board": config.board,
                "graph_names": [graph.name for graph in config.graphs],
                # A loaded config can be deleted from the store while it is
                # running. That is allowed -- deleting a file is not a request
                # to stop an experiment -- so the UI is told, rather than the
                # rig being stopped or the deletion refused.
                "is_still_in_the_store": config.name in stored_names,
            }
            if config is not None
            else None
        ),
        #: Which graph a trial gets when it does not name one. A default for the
        #: caller that has no per-trial opinion -- the web UI's "run a trial" --
        #: and invisible to triald, which names one every time.
        "active_graph": service.active_graph_name,
        "is_open": service.session_opened_at is not None,
        "opened_at_unix_seconds": service.session_opened_at,
        "open_seconds": (
            time.time() - service.session_opened_at
            if service.session_opened_at is not None
            else None
        ),
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
    }


@router.post("/open")
def open_session(request: Request) -> dict:
    """Put the loaded config's graphs on the device.

    The slowest call in this API by a wide margin, and the one where a session
    is allowed to fail: a graph that does not fit this board fails *here*, with
    an animal not yet in the booth, rather than at trial 40.
    """
    service = service_of(request)
    if not service.supervisor.is_connected:
        raise no_device_connected()
    try:
        compiled, elapsed_milliseconds = service.open_session()
    except NoConfigLoaded as exc:
        raise refusal(409, "no_state_machine_config_loaded", str(exc), "state_machine_config")
    except GraphSetCompilationError as exc:
        raise refusal(409, "does_not_fit", str(exc), "graphs")
    except DeviceRefusedTheCommand as exc:
        raise from_device_refusal(exc)
    return {
        "opened": True,
        "state_machine_config": service.require_state_machine_config().name,
        "set_version": compiled.set_version,
        "slots": {graph.name: graph.slot for graph in compiled.graphs_by_slot},
        "pool_usage": compiled.pool_usage,
        "pool_capacity": compiled.pool_capacity,
        "elapsed_milliseconds": elapsed_milliseconds,
    }


@router.post("/close")
def close_session(request: Request) -> dict:
    """End the session. Cancels an armed trial; leaves the set on the board.

    See `RigService.close_session` for why the board is left holding its graphs:
    a committed set surviving is what makes a reconnect cheap, and unloading it
    would buy nothing but a slow start next time.
    """
    return service_of(request).close_session()


class ActiveGraphRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    graph: str


@router.put("/active-graph")
def select_active_graph(request: Request, body: ActiveGraphRequest) -> dict:
    """Choose which graph a trial gets when it does not name one.

    Nothing is pushed and no board is touched: the set is already committed and
    a graph is switched by index at `configure` time, which is the whole reason
    a session uploads a *set*. So this is cheap, and it is safe mid-session.

    A **default, not a mode.** triald names a graph on every `configure` and
    that always wins, so a person switching graphs in a browser cannot change
    what a driven rig is running.
    """
    service = service_of(request)
    try:
        chosen = service.select_active_graph(body.graph)
    except NoConfigLoaded as exc:
        raise refusal(409, "no_state_machine_config_loaded", str(exc), "state_machine_config")
    except GraphNotInTheLoadedConfig as exc:
        raise refusal(409, "graph_not_available", str(exc), "graph")
    return {"active_graph": chosen}


@router.delete("/active-graph")
def clear_active_graph(request: Request) -> dict:
    """Select nothing. A trial must then name its own graph, as triald does."""
    return {"active_graph": service_of(request).select_active_graph(None)}
