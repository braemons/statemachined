# SPDX-License-Identifier: GPL-3.0-or-later
"""Every route the FastAPI app serves has an rpc that will replace it.

**A migration's real risk is not a bad translation; it is a silent omission.**
A route nobody notices is a route nobody misses until a rig needs it, and by
then the app that served it is gone. So while both exist, this holds them to
each other: one table, written down, and a failure the moment a route has no
rpc against its name.

This test is **temporary by design**. It goes when the app does, and what
replaces it is the other direction — the descriptor against the servicers, as
triald's `test_every_rpc_is_implemented.py` does. Until then it is the only
thing that says the interface is complete, because nothing generates from the
proto yet.

The mapping is by hand because it *is* the design: `PATCH /api/device/lines`
becoming `Device/WriteLineMapFile` is a decision, and a checker that derived it
would only be able to check translations nobody had to think about.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from statemachined.daemon.api.application import create_application
from statemachined.daemon.rig_configuration import RigConfiguration

REPOSITORY = Path(__file__).resolve().parents[3]
SERVICE_PROTO = REPOSITORY / "proto" / "statemachined" / "v1" / "service.proto"

#: `(method, path)` → the rpc that takes it over, as `Service/Rpc`.
#:
#: The three static routes have no rpc and never will: a browser asks for
#: `index.html` over HTTP because that is what a browser does, and the edge
#: that serves the panels serves them the same way triald's does.
RPC_FOR_ROUTE = {
    ("GET", "/api/state"): "State/ReadState",
    ("WEBSOCKET", "/api/stream"): "State/WatchState",
    ("GET", "/api/trace"): "State/ReadTrace",
    ("WEBSOCKET", "/api/trace/stream"): "State/WatchTrace",
    ("GET", "/api/trace/trial/{trial_id}"): "State/ReadTrialTrace",
    ("GET", "/api/observers"): "State/ReadObservers",

    ("POST", "/api/trial/configure"): "Trial/Configure",
    ("POST", "/api/trial/start"): "Trial/Start",
    ("POST", "/api/trial/cancel"): "Trial/Cancel",
    ("GET", "/api/trial/result"): "Trial/ReadResult",

    ("GET", "/api/device"): "Device/ReadDevice",
    ("POST", "/api/device/connect"): "Device/OpenLink",
    ("GET", "/api/device/autorun"): "Device/ReadAutorun",
    ("PUT", "/api/device/autorun"): "Device/WriteAutorun",
    ("POST", "/api/device/save"): "Device/SaveSettings",
    ("GET", "/api/device/lines"): "Device/ReadLines",
    ("PATCH", "/api/device/lines"): "Device/WriteLineMapFile",
    ("GET", "/api/device/monitor"): "Device/ReadLineMonitor",
    ("WEBSOCKET", "/api/device/monitor/stream"): "Device/WatchLineMonitor",
    ("GET", "/api/device/firmware"): "Device/ReadFirmware",

    ("GET", "/api/graphs"): "GraphStore/ListGraphs",
    ("GET", "/api/graphs/{graph_name}"): "GraphStore/ReadGraphFile",
    ("PUT", "/api/graphs/{graph_name}"): "GraphStore/WriteGraphFile",
    ("DELETE", "/api/graphs/{graph_name}"): "GraphStore/DeleteGraph",
    ("POST", "/api/graphs/{graph_name}/validate"): "GraphStore/ValidateGraph",
    ("POST", "/api/graphs/{graph_name}/upload"): "GraphStore/UploadGraph",
    ("POST", "/api/session/graphs"): "Session/UploadGraphs",

    ("GET", "/api/session"): "Session/ReadSession",
    ("POST", "/api/session/open"): "Session/Open",
    ("POST", "/api/session/close"): "Session/Close",
    ("PUT", "/api/session/active-graph"): "Session/SetActiveGraph",
    ("DELETE", "/api/session/active-graph"): "Session/ClearActiveGraph",

    ("GET", "/api/state-machine-configs"): "StateMachineConfigStore/ListConfigs",
    ("GET", "/api/state-machine-configs/{config_name}"): "StateMachineConfigStore/ReadConfigFile",
    ("PUT", "/api/state-machine-configs/{config_name}"): "StateMachineConfigStore/WriteConfigFile",
    ("DELETE", "/api/state-machine-configs/{config_name}"): "StateMachineConfigStore/DeleteConfig",
    ("POST", "/api/state-machine-configs/{config_name}/load"): "StateMachineConfigStore/LoadConfig",

    ("GET", "/api/recordings"): "Recording/ReadRecordings",
    ("POST", "/api/recordings/start"): "Recording/Start",
    ("POST", "/api/recordings/pause"): "Recording/Pause",
    ("POST", "/api/recordings/resume"): "Recording/Resume",
    ("POST", "/api/recordings/stop"): "Recording/Stop",
    ("POST", "/api/recordings/clear"): "Recording/Clear",
    ("GET", "/api/recordings/{name}"): "Recording/ReadRecording",
    ("GET", "/api/recordings/{name}/entries"): "Recording/ReadEntries",
    ("DELETE", "/api/recordings/{name}"): "Recording/DeleteRecording",

    ("GET", "/api/health"): "Configuration/ReadHealth",
    ("GET", "/api/config"): "Configuration/ReadConfiguration",
    ("PATCH", "/api/config"): "Configuration/PatchConfiguration",
}

#: Served over HTTP for ever: a browser asks for these by URL.
STATIC_PATHS = {"/", "/elements/{relative_path:path}", "/ui/{relative_path:path}"}

#: FastAPI's own, which go when the app does.
DOCUMENTATION_PATHS = {"/docs", "/redoc", "/docs/oauth2-redirect"}


@pytest.fixture
def routes(tmp_path: Path) -> set[tuple[str, str]]:
    """Every route the app declares, as `(method, path)`.

    The app is built rather than a router imported, because the paths this
    checks are the *mounted* ones — a router's prefix is where half of a route
    lives, and a table checked against un-prefixed paths would check nothing.

    `connect_on_startup` off: there is no board, and the routes are declared
    before anything is opened anyway.
    """
    configuration = RigConfiguration(
        device_target="loop://",
        connect_on_startup=False,
        graph_store_directory=tmp_path / "graphs",
        state_machine_config_directory=tmp_path / "configs",
        trace_directory=tmp_path / "trace",
    )
    return served_routes(create_application(configuration))


def served_routes(application) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    _collect(application.routes, found)
    return found


def _collect(routes, found: set[tuple[str, str]], prefix: str = "") -> None:
    """Walk the route tree, including routers that were not flattened.

    This FastAPI keeps an included router as a node holding the router it
    included rather than splicing its routes in, so a reader that only looked
    one level deep would find the four documentation routes and call it a day —
    which is exactly the vacuous pass `test_the_readers_see_something` exists
    to catch. A router's own prefix is already in its routes' paths; the one
    added at include time is not, so it is carried down.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            _collect(included.routes, found, prefix + (getattr(context, "prefix", "") or ""))
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        path = prefix + path
        if path in STATIC_PATHS or path.startswith("/openapi") or path in DOCUMENTATION_PATHS:
            continue
        methods = getattr(route, "methods", None)
        if not methods:  # a WebSocket route declares none
            found.add(("WEBSOCKET", path))
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            found.add((method, path))


def declared_rpcs() -> set[str]:
    """`Service/Rpc` for every rpc the proto declares."""
    text = SERVICE_PROTO.read_text()
    rpcs = set()
    for service, body in re.findall(r"^service (\w+) \{(.*?)^\}", text, re.M | re.S):
        for rpc in re.findall(r"^\s*rpc (\w+)\(", body, re.M):
            rpcs.add(f"{service}/{rpc}")
    return rpcs


def test_the_readers_see_something(routes: set[tuple[str, str]]) -> None:
    """Both halves are regex-and-reflection, so both can go quiet."""
    assert len(routes) > 30
    assert len(declared_rpcs()) > 30


def test_every_route_has_an_rpc(routes: set[tuple[str, str]]) -> None:
    missing = sorted(route for route in routes if route not in RPC_FOR_ROUTE)
    assert not missing, (
        "these routes have no rpc to replace them:\n"
        + "\n".join(f"  {method} {path}" for method, path in missing)
    )


def test_every_mapped_rpc_exists_in_the_proto() -> None:
    declared = declared_rpcs()
    unknown = sorted({rpc for rpc in RPC_FOR_ROUTE.values() if rpc not in declared})
    assert not unknown, (
        "the table names rpcs the proto does not declare:\n" + "\n".join(f"  {r}" for r in unknown)
    )


def test_the_table_does_not_name_a_route_that_is_gone(routes: set[tuple[str, str]]) -> None:
    """The other way round: a route deleted from the app leaves a stale row,
    and a stale row is how a table stops being read."""
    stale = sorted(route for route in RPC_FOR_ROUTE if route not in routes)
    assert not stale, (
        "the table names routes the app no longer serves:\n"
        + "\n".join(f"  {method} {path}" for method, path in stale)
    )
