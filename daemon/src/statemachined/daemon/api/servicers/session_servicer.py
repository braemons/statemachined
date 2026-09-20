# SPDX-License-Identifier: LGPL-3.0-or-later
"""What this rig is loaded with, and the graph set on the board."""

from __future__ import annotations

from statemachined._proto.statemachined.v1 import service_pb2_grpc
from statemachined.daemon.api import convert
from statemachined.daemon.api.rig_service import RigService

from .refusals import answering, no_board_attached


class SessionServicer(service_pb2_grpc.SessionServicer):
    def __init__(self, service: RigService) -> None:
        self.service = service

    def _session_state(self):
        return convert.session_state_to_wire(
            config=self.service.state_machine_config,
            committed=self.service.supervisor.committed_graph_set,
            opened_at=self.service.session_opened_at,
            active_graph=self.service.active_graph_name,
            stored_config_names=list(
                self.service.state_machine_config_store.stored_config_names()
            ),
        )

    async def ReadSession(self, request, context):
        def body():
            return self._session_state()

        return await answering(context, body)

    async def Open(self, request, context):
        """Put the loaded config's graphs on the device.

        **The slowest call in this API by a wide margin, and the one where a
        session is allowed to fail**: a graph that does not fit this board
        fails here, with an animal not yet in the booth, rather than at trial
        40.
        """

        def body():
            if not self.service.supervisor.is_connected:
                raise no_board_attached()
            compiled, elapsed = self.service.open_session()
            return convert.open_session_result_to_wire(
                compiled,
                state_machine_config=self.service.require_state_machine_config().name,
                elapsed_milliseconds=elapsed,
            )

        return await answering(context, body)

    async def UploadGraphs(self, request, context):
        """The same upload, composed by hand rather than from a config."""

        def body():
            if not self.service.supervisor.is_connected:
                raise no_board_attached()
            compiled, elapsed = self.service.upload_session_graph_set(list(request.graph_names))
            loaded = self.service.state_machine_config
            return convert.open_session_result_to_wire(
                compiled,
                state_machine_config=loaded.name if loaded is not None else "",
                elapsed_milliseconds=elapsed,
            )

        return await answering(context, body)

    async def Close(self, request, context):
        """Give the board up. The committed set stays on it, which is what
        makes a reconnect cheap."""

        def body():
            self.service.close_session()
            return self._session_state()

        return await answering(context, body)

    async def SetActiveGraph(self, request, context):
        def body():
            return convert.active_graph_to_wire(self.service.select_active_graph(request.graph))

        return await answering(context, body)

    async def ClearActiveGraph(self, request, context):
        def body():
            return convert.active_graph_to_wire(self.service.select_active_graph(None))

        return await answering(context, body)
