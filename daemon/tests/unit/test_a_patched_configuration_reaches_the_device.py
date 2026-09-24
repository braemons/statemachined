# SPDX-License-Identifier: GPL-3.0-or-later
"""A patched setting that the device uses reaches the device.

`apply_configuration_changes` wrote every patched field into the rig
configuration and passed two of them on -- the target and the expected board.
The seed and the baud rate stayed in the configuration, where nothing reads
them after startup: `PATCHABLE` says pinning the seed "takes effect on the next
connection", and the next connection greeted with the old one.
"""

from __future__ import annotations

from statemachined.daemon.api.rig_service import RigService
from statemachined.daemon.rig_configuration import RigConfiguration


def a_service(tmp_path) -> RigService:
    return RigService(
        RigConfiguration(
            device_target="loop://",
            graph_store_directory=tmp_path / "graphs",
            state_machine_config_directory=tmp_path / "configs",
            trace_directory=tmp_path / "trace",
            recording_directory=tmp_path / "recordings",
        )
    )


def test_a_pinned_seed_is_the_one_the_next_greeting_sends(tmp_path):
    service = a_service(tmp_path)
    service.apply_configuration_changes({"session_seed": "00C0FFEE00C0FFEE"})
    assert service.supervisor.configured_session_seed == "00C0FFEE00C0FFEE"


def test_clearing_the_seed_goes_back_to_drawing_one(tmp_path):
    service = a_service(tmp_path)
    service.apply_configuration_changes({"session_seed": "00C0FFEE00C0FFEE"})
    service.apply_configuration_changes({"session_seed": ""})
    assert service.supervisor.configured_session_seed is None


def test_a_patched_baud_is_the_one_the_next_link_opens_at(tmp_path):
    service = a_service(tmp_path)
    service.apply_configuration_changes({"device_baud": 9600})
    assert service.supervisor.baud == 9600
