# SPDX-License-Identifier: LGPL-3.0-or-later
"""The trial loop: triald drives, statemachined reports.

dev/API.md §5. Three calls in the critical path of every trial, and they are
small on purpose -- everything that could have been done once per session was
done once per session, in `POST /api/session/graphs`.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ..device.device_supervisor import NoGraphSetCommitted
from ..device.message_framing import DeviceRefusedTheCommand
from ..graph_set_compiler import GraphSetCompilationError
from ..model.trial_record import TrialResultRecord
from .http_errors import from_device_refusal, no_device_connected, refusal
from .rig_service import NoActiveGraph, RigService

router = APIRouter(prefix="/api/trial", tags=["trial"])


def service_of(request: Request) -> RigService:
    return request.app.state.rig_service


class DistributionPatch(BaseModel):
    """A per-trial override of one named distribution's parameters.

    By **name**, like everything else a caller says. The daemon knows which
    entry of the shared pool that name became -- it built the set -- and an
    index on the caller's side would be a cache to get wrong across a re-upload.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    minimum_ms: int | None = None
    maximum_ms: int | None = None
    mean_ms: int | None = None
    duration_ms: int | None = None


class ConfigureTrialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trial_id: int = Field(ge=0)
    #: A name, never a slot. See dev/DAEMON.md §3.1.
    #:
    #: Empty means "the active graph" (`PUT /api/session/active-graph`), which
    #: is what a person pressing a button on a bench means and what triald never
    #: relies on: triald names one every trial, and a name given here always
    #: wins over the selection.
    graph: str = ""
    #: A wall-clock cap on the whole trial. It stays regardless of the graph:
    #: validation cannot tell a ten-second foreperiod from a hang.
    cap_milliseconds: int = Field(default=0, ge=0)
    start_source: str = "serial"
    distribution_patches: list[DistributionPatch] = Field(default_factory=list)


class TrialIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trial_id: int = Field(ge=0)


@router.post("/configure")
def configure_trial(request: Request, body: ConfigureTrialRequest) -> dict:
    """Arm the device for one trial of one graph. Nothing is uploaded.

    `elapsed_milliseconds` is reported anyway, so an arm that took longer than
    it should is a number per trial rather than an inference from a trial that
    armed late.
    """
    service = service_of(request)
    if not service.supervisor.is_connected:
        raise no_device_connected()

    try:
        graph_name = service.graph_for_a_trial(body.graph)
    except NoActiveGraph as exc:
        raise refusal(409, "no_graph_named", str(exc), "graph")

    started = time.monotonic()
    try:
        armed = service.configure_trial(
            trial_id=body.trial_id,
            graph_name=graph_name,
            cap_milliseconds=body.cap_milliseconds,
            start_source=body.start_source,
            distribution_patches=_patches_as_wire_fields(service, body, graph_name),
        )
    except NoGraphSetCommitted as exc:
        raise refusal(409, "no_graph_set", str(exc), "graph")
    except GraphSetCompilationError as exc:
        # The name is not in the committed set. A session that declared its
        # graphs and then asks for a fourth has a misconfigured trial type, and
        # the refusal says which name and which set.
        raise refusal(409, "graph_not_in_set", str(exc), "graph")
    except DeviceRefusedTheCommand as exc:
        raise from_device_refusal(exc)

    return {
        "trial_id": body.trial_id,
        "graph": graph_name,
        "set_version": armed.get("set_version"),
        "graph_index": armed.get("graph_index"),
        "elapsed_milliseconds": int((time.monotonic() - started) * 1000),
    }


def _patches_as_wire_fields(
    service: RigService, body: ConfigureTrialRequest, graph_name: str
) -> list[dict]:
    """Named distributions into pool indices and the wire's `a`/`b`/`c`.

    The same translation `graph_set_compiler` does for an upload, and it has to
    happen here too because a patch names a distribution of a graph that is
    already on the device.
    """
    if not body.distribution_patches:
        return []
    compiled = service.supervisor.committed_graph_set
    if compiled is None:
        raise NoGraphSetCommitted("no graph set is committed, so no distribution has an index")
    compiled_graph = compiled.graph_named(graph_name)

    wire_patches = []
    for patch in body.distribution_patches:
        if patch.name not in compiled_graph.distribution_pool_index_by_name:
            known = ", ".join(sorted(compiled_graph.distribution_pool_index_by_name))
            raise GraphSetCompilationError(
                f"graph {graph_name!r} has no distribution called {patch.name!r}. Has: {known}"
            )
        fields: dict[str, object] = {
            "i": compiled_graph.distribution_pool_index_by_name[patch.name]
        }
        # Positional on the wire by `kind`; named here. Only a, b and c may be
        # patched, and never `kind` -- that would be a different graph.
        if patch.duration_ms is not None:
            fields["a"] = patch.duration_ms
        if patch.minimum_ms is not None:
            fields["a"] = patch.minimum_ms
        if patch.maximum_ms is not None:
            fields["b"] = patch.maximum_ms
        if patch.mean_ms is not None:
            fields["c"] = patch.mean_ms
        wire_patches.append(fields)
    return wire_patches


@router.post("/start")
def start_trial(request: Request, body: TrialIdentity) -> dict:
    """Start the armed trial. Refused unless the device is armed for that id."""
    service = service_of(request)
    if not service.supervisor.is_connected:
        raise no_device_connected()
    try:
        started = service.start_trial(body.trial_id)
    except DeviceRefusedTheCommand as exc:
        raise from_device_refusal(exc)
    return {"trial_id": body.trial_id, "started_device_microseconds": started.get("at_us")}


@router.post("/cancel")
def cancel_trial(request: Request, body: TrialIdentity) -> dict:
    """Ask for a cancel, and report whatever actually happened.

    A cancel that races a terminal state comes back with the **real** outcome.
    That is passed through: asking to cancel and being told `HIT` is triald's to
    cope with, and the alternative is a record claiming a trial was cancelled
    when the animal had already responded.
    """
    service = service_of(request)
    if not service.supervisor.is_connected:
        raise no_device_connected()
    try:
        cancel_ack = service.cancel_trial(body.trial_id)
    except DeviceRefusedTheCommand as exc:
        raise from_device_refusal(exc)
    return {
        "trial_id": body.trial_id,
        "cancelled": cancel_ack.get("cancelled"),
        "outcome_code": cancel_ack.get("outcome"),
    }


@router.get("/result")
def read_last_trial_result(request: Request) -> TrialResultRecord:
    """The last completed trial, named against the graph that actually ran."""
    result = service_of(request).last_trial_result
    if result is None:
        raise refusal(404, "no_result_yet", "no trial has completed on this connection", "trial")
    return result
