# SPDX-License-Identifier: AGPL-3.0-or-later
"""One trial, from arming to result.

**The loop.** triald drives it and a person on a bench drives the same four
rpcs; there is no private path for either
(`contracts/INTERACTIONS.md` §2).
"""

from __future__ import annotations

from statemachined._proto.statemachined.v1 import service_pb2, service_pb2_grpc, trial_pb2
from statemachined.daemon.api import convert
from statemachined.daemon.api.rig_service import RigService

from .refusals import Category, Refusal, answering, no_board_attached


class TrialServicer(service_pb2_grpc.TrialServicer):
    def __init__(self, service: RigService) -> None:
        self.service = service

    async def Configure(self, request, context):
        def body():
            if not self.service.supervisor.is_connected:
                raise no_board_attached()
            arguments = convert.configure_trial_from_wire(request)
            # An empty name means the active graph, and *which* graph that is
            # is the session's business rather than the seam's.
            arguments["graph_name"] = self.service.graph_for_a_trial(arguments["graph_name"])
            armed = self.service.configure_trial(**arguments)
            return trial_pb2.ConfigureTrialResult(
                trial_id=request.trial_id,
                graph=arguments["graph_name"],
                set_version=int(armed.get("set_version") or 0),
                graph_index=int(armed.get("graph_index") or 0),
                elapsed_milliseconds=int(armed.get("elapsed_milliseconds") or 0),
            )

        return await answering(context, body)

    async def Start(self, request, context):
        def body():
            if not self.service.supervisor.is_connected:
                raise no_board_attached()
            started = self.service.start_trial(request.trial_id)
            return trial_pb2.StartTrialResult(
                trial_id=request.trial_id,
                started_device_microseconds=int(started.get("at_us") or 0),
            )

        return await answering(context, body)

    async def Cancel(self, request, context):
        def body():
            if not self.service.supervisor.is_connected:
                raise no_board_attached()
            acknowledgement = self.service.cancel_trial(request.trial_id)
            # **A cancel that races a terminal state comes back with the real
            # outcome**, and it is passed through rather than rewritten to
            # CANCELLED: a record claiming a trial was cancelled when the
            # animal had already responded is worse than a surprising answer.
            return service_pb2.CancelTrialResult(
                trial_id=request.trial_id,
                cancelled=bool(acknowledgement.get("cancelled")),
                outcome_code=int(acknowledgement.get("outcome") or 0),
            )

        return await answering(context, body)

    async def ReadResult(self, request, context):
        """The last completed trial, named against the graph that actually ran.

        **Only the last one.** This daemon keeps one result, not a history —
        the trace is the history, and `State/ReadTrialTrace` is how a caller
        asks about a trial that is not the most recent. So naming an older
        `trial_id` is refused rather than answered with the wrong trial, which
        is the mistake the whole addressing scheme exists to prevent.
        """

        def body():
            result = self.service.last_trial_result
            if result is None:
                raise Refusal(
                    Category.WRONG_MOMENT,
                    "no_result_yet",
                    "no trial has completed on this connection",
                    "trial",
                )
            if request.HasField("trial_id") and request.trial_id != result.trial_id:
                raise Refusal(
                    Category.NO_SUCH_THING,
                    "not_the_last_trial",
                    f"trial {request.trial_id} is not the last one to complete; "
                    f"that was trial {result.trial_id}",
                    "trial_id",
                )
            return convert.trial_result_to_wire(result)

        return await answering(context, body)
