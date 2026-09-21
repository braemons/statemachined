# SPDX-License-Identifier: AGPL-3.0-or-later
"""The state-machine configs: one experiment's graphs and line map, as a file."""

from __future__ import annotations

from statemachined._proto.statemachined.v1 import service_pb2_grpc
from statemachined.daemon.api import convert
from statemachined.daemon.api.rig_service import RigService
from statemachined.model.state_machine_config import StateMachineConfig

from .refusals import Category, Refusal, answering


class StateMachineConfigStoreServicer(service_pb2_grpc.StateMachineConfigStoreServicer):
    def __init__(self, service: RigService) -> None:
        self.service = service

    def _summaries(self):
        loaded = self.service.state_machine_config
        return convert.config_summaries_to_wire(
            self.service.state_machine_config_store.summaries(),
            loaded=loaded.name if loaded is not None else None,
        )

    async def ListConfigs(self, request, context):
        def body():
            return self._summaries()

        return await answering(context, body)

    async def ReadConfigFile(self, request, context):
        def body():
            config = self.service.state_machine_config_store.load(request.name)
            return convert.stored_file_to_wire(
                config.name, config.model_dump_json(indent=2, exclude_none=True)
            )

        return await answering(context, body)

    async def WriteConfigFile(self, request, context):
        def body():
            try:
                config = StateMachineConfig.model_validate_json(request.text)
            except ValueError as problem:
                raise Refusal(
                    Category.BAD_REQUEST, "bad_state_machine_config", str(problem), "text"
                ) from problem
            if request.name and config.name != request.name:
                raise Refusal(
                    Category.BAD_REQUEST,
                    "config_name_mismatch",
                    f"the file calls this config {config.name!r} and it was stored as "
                    f"{request.name!r}",
                    "name",
                )
            self.service.save_state_machine_config(config)
            # The whole store, as `DeleteConfig` answers: `loaded` beside the
            # listing is what says whether this write landed on the config the
            # rig is running, and **saving is not loading** — a UI that could
            # only save by also arming the rig is a UI nobody edits during a
            # session.
            return self._summaries()

        return await answering(context, body)

    async def DeleteConfig(self, request, context):
        def body():
            self.service.state_machine_config_store.delete(request.name)
            return self._summaries()

        return await answering(context, body)

    async def LoadConfig(self, request, context):
        """Make one the rig's: apply its line map, and push the wiring.

        The graphs are **not** uploaded here — `Session/Open` does that,
        because that is the slow call and this one should not be. Refused while
        a trial is armed or running, like every other change to the wiring: a
        line map is what a trial's record *means*, and moving it mid-trial
        makes that record a fiction.
        """

        def body():
            report = self.service.read_device_state()
            if report.get("running") or report.get("link_state") == 2:
                raise Refusal(
                    Category.WRONG_MOMENT, "busy", "a trial is armed or running", "config_name"
                )
            config = self.service.load_state_machine_config(request.name)
            supervisor = self.service.supervisor
            return convert.loaded_config_result_to_wire(
                loaded=config.name,
                wiring_pushed=supervisor.is_connected,
                line_map=convert.line_map_view_to_wire(
                    supervisor.resolved_line_map,
                    input_word=None,
                    output_word=None,
                    pin_labels_came_from=supervisor.pin_map.source,
                    board_input_pins=list(supervisor.pin_map.input_pin_labels),
                    board_output_pins=list(supervisor.pin_map.output_pin_labels),
                ),
                graph_names=[graph.name for graph in config.graphs],
            )

        return await answering(context, body)
