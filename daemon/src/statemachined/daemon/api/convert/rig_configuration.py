# SPDX-License-Identifier: LGPL-3.0-or-later
"""The box's own settings.

**The rig config, never a state-machine config.** The two are unrelated and
both were once called "config": this one changes when the hardware does and
lives in `/etc/braemons`, and a state-machine config is one experiment's graphs
and line map (`contracts/DAEMON_LAYOUT.md` §1).

**Nothing here writes `/etc`.** A patch lasts until the daemon restarts, and
the message says so out loud in a field. That file belongs to whoever set the
box up: an API that rewrote it would make the running daemon the authority on
what the hardware is, and would silently diverge from the conffile the next
upgrade compares against.
"""

from __future__ import annotations

from typing import Any

from statemachined._proto.statemachined.v1 import rig_configuration_pb2

#: The fields a caller may change while the daemon runs. Everything else in the
#: configuration decides something that has already happened — where the trace
#: ring lives, which port is bound — and changing it would leave a daemon whose
#: state does not match its own description.
PATCHABLE = (
    "device_target",
    "device_baud",
    "expected_board",
    "graph_mode",
    "startup_state_machine_config",
    # Pinning the seed is what somebody does to reproduce a session, and it is
    # a setting rather than a consequence: it takes effect on the next
    # connection, so nothing that has already happened depends on it.
    "session_seed",
)


def rig_configuration_to_wire(
    configuration: Any,
) -> rig_configuration_pb2.RigConfiguration:
    """Every setting, with the paths as text.

    A `Path` is a host thing; a client on another machine gets a string it can
    show and must not try to open.
    """
    return rig_configuration_pb2.RigConfiguration(
        device_target=configuration.device_target,
        device_baud=configuration.device_baud,
        device_timeout_seconds=configuration.device_timeout_seconds,
        expected_board=configuration.expected_board,
        session_seed=configuration.session_seed,
        connect_on_startup=configuration.connect_on_startup,
        startup_state_machine_config=configuration.startup_state_machine_config,
        graph_mode=configuration.graph_mode,
        trace_ring_entries=configuration.trace_ring_entries,
        heartbeat_seconds=configuration.heartbeat_seconds,
        trace_directory=str(configuration.trace_directory),
        graph_store_directory=str(configuration.graph_store_directory),
        recording_directory=str(configuration.recording_directory),
        state_machine_config_directory=str(configuration.state_machine_config_directory),
    )


def rig_configuration_patch_from_wire(
    patch: rig_configuration_pb2.RigConfigurationPatch,
) -> dict[str, Any]:
    """Only the fields that were set.

    Every field is `optional` so that absent means "leave it alone", which is a
    question protobuf can only answer for a field that tracks presence — and
    `expected_board=""` is a real setting meaning "accept whatever answers".
    """
    return {name: getattr(patch, name) for name in PATCHABLE if patch.HasField(name)}


def rig_configuration_update_to_wire(
    configuration: Any, *, reconnected: bool
) -> rig_configuration_pb2.RigConfigurationUpdate:
    return rig_configuration_pb2.RigConfigurationUpdate(
        configuration=rig_configuration_to_wire(configuration),
        reconnected=reconnected,
        until_restart=True,
    )
