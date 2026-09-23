# SPDX-License-Identifier: GPL-3.0-or-later
"""A startup config that does not load is reported in a sentence, not a repr.

`GraphNotInStore` and `ConfigNotInStore` are `KeyError`s, and `str()` of a
`KeyError` is its message with a repr's quotes around it -- which is how
`last_error` came to read `did not load: "no state-machine config called
'nope' is stored..."`. `refusals._sentence` already takes the argument
directly for the rpcs; this is the one place that formatted the exception
itself.
"""

from __future__ import annotations

from statemachined.daemon.api.rig_service import RigService
from statemachined.daemon.rig_configuration import RigConfiguration


def test_the_reason_reads_as_the_sentence_the_store_wrote(tmp_path):
    service = RigService(
        RigConfiguration(
            device_target="loop://",
            graph_store_directory=tmp_path / "graphs",
            state_machine_config_directory=tmp_path / "configs",
            trace_directory=tmp_path / "trace",
            recording_directory=tmp_path / "recordings",
            startup_state_machine_config="nope",
        )
    )
    service._load_the_startup_config()
    assert service.last_error_from_the_device == (
        "the startup state-machine config 'nope' did not load: "
        "no state-machine config called 'nope' is stored. Stored: (none)"
    )
