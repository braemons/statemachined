# SPDX-License-Identifier: AGPL-3.0-or-later
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

    def _summaries(self) -> list[dict]:
        """Every stored graph, with enough of each to choose between them.

        A graph that no longer parses is listed as broken rather than omitted:
        a paradigm that has silently vanished from the list is how somebody
        spends an afternoon looking for it.
        """
        summaries: list[dict] = []
        for name in self.service.graph_store.stored_graph_names():
            try:
                graph = self.service.graph_store.load(name)
            except Exception as problem:
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
        return summaries

    async def ListGraphs(self, request, context):
        return await answering(
            context, lambda: convert.graph_summaries_to_wire(self._summaries())
        )

    async def ReadGraphFile(self, request, context):
        """The graph as a file, for an editor.

        **`exclude_none` and not `exclude_defaults`.** It was the latter, and
        the file this answered with could not be sent back: a discriminator
        like `kind: Literal["exponential"] = "exponential"` *has* a default, so
        it was dropped, and the graph an editor downloaded no longer said what
        kind its distributions were. Re-uploading it failed with
        `union_tag_not_found` on a field the person had never touched.

        `exclude_none` drops the optionals that were never set, which is what
        was wanted -- a readable file -- without dropping anything the parser
        needs.
        """

        def body():
            graph = self.service.graph_store.load(request.name)
            return convert.stored_file_to_wire(
                graph.name, graph.model_dump_json(indent=2, exclude_none=True)
            )

        return await answering(context, body)

    async def WriteGraphFile(self, request, context):
        def body():
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
        """Remove a graph from the store — unless a session is running it.

        **The guard is the point.** The board holds the compiled set, so
        deleting the file does not stop a trial; what it does is make the
        paradigm that ran unreproducible, halfway through the session that ran
        it. A trial could still name it, and then the record would point at a
        graph nobody can read.
        """

        def body():
            self._refuse_if_the_session_is_running(request.name)
            self.service.graph_store.delete(request.name)
            return convert.graph_summaries_to_wire(self._summaries())

        return await answering(context, body)

    def _refuse_if_the_session_is_running(self, graph_name: str) -> None:
        committed = self.service.supervisor.committed_graph_set
        if committed is not None and any(
            graph.name == graph_name for graph in committed.graphs_by_slot
        ):
            raise Refusal(
                Category.WRONG_MOMENT,
                "graph_in_use",
                f"{graph_name!r} is in the committed set and a trial could still name it",
                "name",
            )

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
        def body():
            graph = self.service.graph_store.load(request.name)
            return convert.graph_validation_to_wire(self._validate(graph))

        return await answering(context, body)

    async def ValidateGraphFile(self, request, context):
        """The same check against a **draft**: what somebody is typing.

        It goes through this daemon's own parser and compiler, so what comes
        back is the refusal the real thing would give — never a second, looser
        description of a graph living in a browser.
        """

        def body():
            return convert.graph_validation_to_wire(self._validate(_parse(request.text)))

        return await answering(context, body)

    async def UploadGraph(self, request, context):
        """Put one graph on the board on its own — a bench convenience.

        **Refused while a session's set is committed**, because the board holds
        one set and this replaces it: losing a session's paradigms because
        somebody previewed a graph is not a recoverable mistake. A set of one
        is not a session, so that case is allowed through.
        """

        def body():
            if not self.service.supervisor.is_connected:
                raise no_board_attached()
            committed = self.service.supervisor.committed_graph_set
            if committed is not None and len(committed.graphs_by_slot) > 1:
                raise Refusal(
                    Category.WRONG_MOMENT,
                    "session_set_committed",
                    f"a session's set of {len(committed.graphs_by_slot)} graphs is "
                    "committed; uploading one graph would replace it",
                    "name",
                )
            compiled, _elapsed = self.service.upload_session_graph_set([request.name])
            return convert.committed_set_to_wire(compiled)

        return await answering(context, body)
