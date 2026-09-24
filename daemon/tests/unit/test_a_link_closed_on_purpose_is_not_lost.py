# SPDX-License-Identifier: GPL-3.0-or-later
"""A link the daemon closed itself is not written down as lost.

The link thread checks for a connection without the device lock, then takes
the lock and pumps. A request can close the link in between -- a re-greeting
that finds the wrong board disconnects on purpose -- and the pump then raised
`DeviceNotConnected`, which went into the trace as `link_lost`.
"""

from __future__ import annotations

from statemachined.daemon.api.rig_service import RigService
from statemachined.daemon.rig_configuration import RigConfiguration
from statemachined.device.state_visit_trace import KIND_LINK_LOST


def test_a_pass_that_finds_no_link_under_the_lock_writes_nothing(tmp_path):
    service = RigService(
        RigConfiguration(
            device_target="loop://",
            graph_store_directory=tmp_path / "graphs",
            state_machine_config_directory=tmp_path / "configs",
            trace_directory=tmp_path / "trace",
            recording_directory=tmp_path / "recordings",
        )
    )
    # What the thread meets when the link closed after its unlocked check.
    assert not service.supervisor.is_connected
    service._read_the_link_once()
    assert [entry["kind"] for entry in service.trace.entries_since(0)] == []
    assert KIND_LINK_LOST not in {entry["kind"] for entry in service.trace.entries_since(0)}
    assert service.last_error_from_the_device is None
