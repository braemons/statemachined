# SPDX-License-Identifier: LGPL-3.0-or-later
"""The graph documents this rig holds.

**A graph is a file**, so these carry its text and the model that parses it is
the only description of one (`proto/statemachined/v1/documents.proto`).
"""

from __future__ import annotations

from statemachined._proto.statemachined.v1 import service_pb2_grpc
from statemachined.daemon.api import convert
from statemachined.daemon.api.rig_service import RigService
from statemachined.graph_set_compiler import compile_graph_set_for_device
from statemachined.model.graph_definition import GraphDefinition

from .refusals import Category, Refusal, answering, no_board_attached


def _parse(text: str) -> GraphDefinition:
    try:
        return GraphDefinition.model_validate_json(text)
    except ValueError as problem:
        raise Refusal(Category.BAD_REQUEST, "bad_graph", str(problem), "text") from problem


class GraphStoreServicer(service_pb2_grpc.GraphStoreServicer):
    def __init__(self, service: RigService) -> None:
        self.service = service

    async def ListGraphs(self, request, context):
        async def body():
            summaries = []
            for name in self.service.graph_store.stored_graph_names():
                try:
                    graph = self.service.graph_store.load(name)
                except Exception as problem:
                    # Listed as broken rather than omitted: a paradigm that has
                    # silently vanished from the list is how somebody spends an
                    # afternoon looking for it.
                    summaries.append({"name": name, "readable": False, "detail": str(problem)})
                    continue
                summaries.append(
                    {
                        "name": graph.name,
                        "readable": True,
                        "state_count": len(graph.states),
                        "entry": graph.entry,
                    }
                )
            return convert.graph_summaries_to_wire(summaries)

        return await answering(context, body)

    async def ReadGraphFile(self, request, context):
        async def body():
            graph = self.service.graph_store.load(request.name)
            return convert.stored_file_to_wire(
                graph.name, graph.model_dump_json(indent=2, exclude_defaults=True)
            )

        return await answering(context, body)

    async def WriteGraphFile(self, request, context):
        async def body():
            graph = _parse(request.text)
            if request.name and graph.name != request.name:
                raise Refusal(
                    Category.BAD_REQUEST,
                    "graph_name_mismatch",
                    f"the file calls this graph {graph.name!r} and it was stored as "
                    f"{request.name!r}",
                    "name",
                )
            self.service.graph_store.save(graph)
            return convert.graph_summary_to_wire(
                {
                    "name": graph.name,
                    "readable": True,
                    "state_count": len(graph.states),
                    "entry": graph.entry,
                }
            )

        return await answering(context, body)

    async def DeleteGraph(self, request, context):
        async def body():
            self.service.graph_store.delete(request.name)
            return await self.ListGraphs(request, context)

        return await answering(context, body)

    def _validate(self, graph: GraphDefinition) -> dict:
        """Every rule, plus **this board's** capacities.

        The capacity half is the half worth having — "you have room for two
        more graphs" is what somebody setting up a session wants to know —
        which is why this needs a board and refuses `unavailable` without one.

        Compiled against the *resolved* map, the same one an upload would use:
        a graph checked against the configured indices and uploaded against the
        board's would be checked against something that never runs.
        """
        supervisor = self.service.supervisor
        if supervisor.capabilities is None:
            raise no_board_attached(
                "a graph is checked against a board's caps, and there is none"
            )
        from statemachined.graph_set_compiler import GraphSetCompilationError

        try:
            compiled = compile_graph_set_for_device(
                [graph], supervisor.resolved_line_map, supervisor.capabilities, 0
            )
        except GraphSetCompilationError as problem:
            return {"valid": False, "detail": str(problem), "warnings": graph.warnings()}
        return {
            "valid": True,
            "pool_usage": compiled.pool_usage,
            "pool_capacity": compiled.pool_capacity,
            # Legal, uploads, runs — and probably narrower than its author
            # thinks. Reported rather than refused.
            "warnings": graph.warnings(),
        }

    async def ValidateGraph(self, request, context):
        async def body():
            graph = self.service.graph_store.load(request.name)
            return convert.graph_validation_to_wire(self._validate(graph))

        return await answering(context, body)

    async def ValidateGraphFile(self, request, context):
        """The same check against a **draft**: what somebody is typing.

        It goes through this daemon's own parser and compiler, so what comes
        back is the refusal the real thing would give — never a second, looser
        description of a graph living in a browser.
        """

        async def body():
            return convert.graph_validation_to_wire(self._validate(_parse(request.text)))

        return await answering(context, body)

    async def UploadGraph(self, request, context):
        async def body():
            if not self.service.supervisor.is_connected:
                raise no_board_attached()
            compiled, _elapsed = self.service.upload_session_graph_set([request.name])
            return convert.committed_set_to_wire(compiled)

        return await answering(context, body)
