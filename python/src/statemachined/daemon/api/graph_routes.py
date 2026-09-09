# SPDX-License-Identifier: LGPL-3.0-or-later
"""The store, the validator, and the one call that uploads a session's set.

docs/reference/api.md §4. `POST /api/session/graphs` is the important one and the slow one:
it is where a session is allowed to fail, minutes before an animal is in the
booth, rather than at trial 40.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ...device.message_framing import DeviceRefusedTheCommand
from ...graph_set_compiler import (
    GraphSetCompilationError,
    compile_graph_set_for_device,
)
from ..graph_store import GraphNotInStore
from ...model.graph_definition import GraphDefinition
from .http_errors import from_device_refusal, no_device_connected, refusal
from .rig_service import RigService

router = APIRouter(prefix="/api", tags=["graphs"])


def service_of(request: Request) -> RigService:
    return request.app.state.rig_service


class SessionGraphNames(BaseModel):
    """The graphs a session will use, in the order that becomes their slots."""

    model_config = ConfigDict(extra="forbid")
    graph_names: list[str] = Field(min_length=1)


@router.get("/graphs")
def list_stored_graphs(request: Request) -> dict:
    service = service_of(request)
    graphs = []
    for graph_name in service.graph_store.stored_graph_names():
        try:
            graph = service.graph_store.load(graph_name)
        except Exception as exc:  # noqa: BLE001
            # A graph that no longer parses is listed as broken rather than
            # omitted: a paradigm that has silently vanished from the list is
            # how somebody spends an afternoon looking for it.
            graphs.append({"name": graph_name, "readable": False, "detail": str(exc)})
            continue
        graphs.append(
            {
                "name": graph.name,
                "readable": True,
                "state_count": len(graph.states),
                "entry": graph.entry,
            }
        )
    return {"graphs": graphs}


@router.get("/graphs/{graph_name}")
def read_stored_graph(request: Request, graph_name: str) -> GraphDefinition:
    try:
        return service_of(request).graph_store.load(graph_name)
    except GraphNotInStore as exc:
        raise refusal(404, "no_such_graph", str(exc), "graph_name")


@router.put("/graphs/{graph_name}")
def write_stored_graph(request: Request, graph_name: str, graph: GraphDefinition) -> dict:
    """Store one. Validated on the way in, so the store never holds a bad graph."""
    if graph.name != graph_name:
        raise refusal(
            409,
            "name_mismatch",
            f"the path says {graph_name!r} and the graph calls itself {graph.name!r}",
            "name",
        )
    path = service_of(request).graph_store.save(graph)
    return {"name": graph.name, "path": str(path)}


@router.delete("/graphs/{graph_name}")
def delete_stored_graph(request: Request, graph_name: str) -> dict:
    service = service_of(request)
    committed = service.supervisor.committed_graph_set
    if committed is not None and any(
        graph.name == graph_name for graph in committed.graphs_by_slot
    ):
        raise refusal(
            409,
            "graph_in_use",
            f"{graph_name!r} is in the committed set and a trial could still name it",
            "graph_name",
        )
    try:
        service.graph_store.delete(graph_name)
    except GraphNotInStore as exc:
        raise refusal(404, "no_such_graph", str(exc), "graph_name")
    return {"deleted": graph_name}


@router.post("/graphs/{graph_name}/validate")
def validate_stored_graph(request: Request, graph_name: str) -> dict:
    """Every rule, plus **this device's** caps. Changes nothing, uploads nothing.

    The capacity half is the useful half: "you have room for two more graphs" is
    what somebody setting up a session wants to know.
    """
    service = service_of(request)
    if service.supervisor.capabilities is None:
        raise no_device_connected("a graph is checked against a board's caps, and there is none")
    try:
        graph = service.graph_store.load(graph_name)
    except GraphNotInStore as exc:
        raise refusal(404, "no_such_graph", str(exc), "graph_name")

    try:
        compiled = compile_graph_set_for_device(
            # The resolved map, the same one an upload would use: a graph
            # checked against the configured indices and uploaded against the
            # board's would be checked against something that never runs.
            [graph], service.supervisor.resolved_line_map, service.supervisor.capabilities, 0
        )
    except GraphSetCompilationError as exc:
        return {"valid": False, "detail": str(exc), "warnings": graph.warnings()}
    return {
        "valid": True,
        "pool_usage": compiled.pool_usage,
        "pool_capacity": compiled.pool_capacity,
        # Legal, uploads, runs -- and probably narrower than its author thinks.
        # Reported rather than refused: see GraphDefinition.warnings().
        "warnings": graph.warnings(),
    }


@router.post("/graphs/{graph_name}/upload")
def upload_one_graph_as_a_set_of_one(request: Request, graph_name: str) -> dict:
    """Try a graph out. The bench and UI path.

    Refused while a session's set is committed: losing a session's paradigms
    because somebody previewed a graph is not a recoverable mistake, and the
    device holds one set.
    """
    service = service_of(request)
    committed = service.supervisor.committed_graph_set
    if committed is not None and len(committed.graphs_by_slot) > 1:
        raise refusal(
            409,
            "session_set_committed",
            "a session's set of "
            f"{len(committed.graphs_by_slot)} graphs is committed; uploading one graph "
            "would replace it",
            "graph_name",
        )
    return _upload(service, [graph_name])


@router.post("/session/graphs")
def upload_the_sessions_graph_set(request: Request, body: SessionGraphNames) -> dict:
    """Every graph a session will use, uploaded once, before the first trial.

    The slowest call in this API by a wide margin, and the one where a session
    is allowed to fail. See docs/reference/api.md §4.
    """
    return _upload(service_of(request), body.graph_names)


def _upload(service: RigService, graph_names: list[str]) -> dict:
    if not service.supervisor.is_connected:
        raise no_device_connected()
    try:
        compiled, elapsed_milliseconds = service.upload_session_graph_set(graph_names)
    except GraphNotInStore as exc:
        raise refusal(404, "no_such_graph", str(exc), "graph_names")
    except GraphSetCompilationError as exc:
        raise refusal(409, "does_not_fit", str(exc), "graph_names")
    except DeviceRefusedTheCommand as exc:
        raise from_device_refusal(exc)
    return {
        "set_version": compiled.set_version,
        "slots": {graph.name: graph.slot for graph in compiled.graphs_by_slot},
        "pool_usage": compiled.pool_usage,
        "pool_capacity": compiled.pool_capacity,
        "elapsed_milliseconds": elapsed_milliseconds,
    }
