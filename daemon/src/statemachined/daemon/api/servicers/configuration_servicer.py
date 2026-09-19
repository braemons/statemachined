# SPDX-License-Identifier: LGPL-3.0-or-later
"""The box's own settings, and whether it is up."""

from __future__ import annotations

from statemachined._proto.statemachined.v1 import service_pb2, service_pb2_grpc
from statemachined.daemon.api import convert
from statemachined.daemon.api.rig_service import RigService

from .refusals import Category, Refusal, answering


class ConfigurationServicer(service_pb2_grpc.ConfigurationServicer):
    def __init__(self, service: RigService) -> None:
        self.service = service

    async def ReadConfiguration(self, request, context):
        def body():
            return convert.rig_configuration_to_wire(self.service.configuration)

        return await answering(context, body)

    async def PatchConfiguration(self, request, context):
        """Change it, and do whatever the change implies.

        Refused while a trial is armed or running: a target that changed means
        a *different device*, and swapping the device under a running trial
        would move the thing the trial is being measured against.

        **Until the daemon restarts.** Nothing here writes `/etc/braemons` —
        that file belongs to whoever set the box up, and an API that rewrote it
        would make the running daemon the authority on what the hardware is.
        """

        def body():
            report = self.service.read_device_state()
            if report.get("running") or report.get("link_state") == 2:
                raise Refusal(
                    Category.WRONG_MOMENT,
                    "busy",
                    "a trial is armed or running",
                    "configuration",
                )
            changes = convert.rig_configuration_patch_from_wire(request)
            reconnected = self.service.apply_configuration_changes(changes)
            return convert.rig_configuration_update_to_wire(
                self.service.configuration, reconnected=reconnected
            )

        return await answering(context, body)

    async def ReadHealth(self, request, context):
        """Is the daemon up, and does it have a board.

        **Two different questions, and a supervisor only asks the first**: a
        daemon whose board is unplugged is still the thing you ask *why*. That
        is why this answers rather than refusing with no device, and why it
        touches nothing that could hang on a serial port that is not draining.
        """

        def body():
            return service_pb2.Health(
                ok=True, device_connected=self.service.supervisor.is_connected
            )

        return await answering(context, body)
