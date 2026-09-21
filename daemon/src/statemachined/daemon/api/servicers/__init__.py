# SPDX-License-Identifier: AGPL-3.0-or-later
"""One module per service in `proto/statemachined/v1/service.proto`.

**A servicer holds no logic.** It decodes, calls `RigService`, and encodes —
the decision is the service's and the shape is `api/convert/`'s. Anything a
servicer worked out would be a second opinion about a rig that already has one,
and the routes it replaces were thin for the same reason.

Every method runs inside `answering`, so a domain exception raised six frames
down reaches the caller as the refusal it is rather than as an `INTERNAL` with
a traceback in the log — and so that adding a servicer does not mean
remembering what to catch.
"""

from __future__ import annotations

from typing import Any

from statemachined.daemon.api.rig_service import RigService

from .configuration_servicer import ConfigurationServicer
from .device_servicer import DeviceServicer
from .graph_store_servicer import GraphStoreServicer
from .recording_servicer import RecordingServicer
from .session_servicer import SessionServicer
from .state_machine_config_store_servicer import StateMachineConfigStoreServicer
from .state_servicer import StateServicer
from .trial_servicer import TrialServicer

#: Keyed by the proto's own service name, so `grpc_server.py` and the browser
#: edge can both build the same table from the descriptor rather than from a
#: list either of them could get wrong.
SERVICER_CLASSES: dict[str, Any] = {
    "State": StateServicer,
    "Trial": TrialServicer,
    "Device": DeviceServicer,
    "GraphStore": GraphStoreServicer,
    "StateMachineConfigStore": StateMachineConfigStoreServicer,
    "Session": SessionServicer,
    "Recording": RecordingServicer,
    "Configuration": ConfigurationServicer,
}


def build_servicers(service: RigService) -> dict[str, Any]:
    return {name: servicer(service) for name, servicer in SERVICER_CLASSES.items()}


__all__ = [
    "SERVICER_CLASSES",
    "ConfigurationServicer",
    "DeviceServicer",
    "GraphStoreServicer",
    "RecordingServicer",
    "SessionServicer",
    "StateMachineConfigStoreServicer",
    "StateServicer",
    "TrialServicer",
    "build_servicers",
]
