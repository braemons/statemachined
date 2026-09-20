# SPDX-License-Identifier: LGPL-3.0-or-later
"""The seam between what this daemon thinks in and what it says.

**Every conversion between this daemon's own types and the generated wire
types is here, and nowhere else.** Not in a servicer, not in `model/`, not
beside the code that happened to need it. A conversion that lives next to its
caller is a second, quieter description of the interface, and the whole point
of authoring the interface in one place is that there is no second one.

Names are `x_from_wire` and `x_to_wire`, in that direction and nothing else, so
the direction of a call is readable rather than looked up. triald's
`api/convert/` and mousewheeld's `daemon/src/convert/` are the same
arrangement, and reading one should teach you the others.

**Why a seam at all**, when several of these are field-for-field:

* `model/` holds documents — a graph, a line map, a config — with validators,
  defaults and invariants that refuse a bad file by field name. A protobuf
  message is a mutable bag of fields with no opinion, and making the daemon
  think in one would cost every one of those. The standing rule: **proto
  messages are never internal data.**
* proto3 has no required fields, so a message arriving with nothing set is
  valid. Turning that into either a default or a refusal is work with exactly
  one right place to happen.
* An enum on the wire may hold a number this build has never heard of. Reading
  it as the first variant is how a newer client's trial gets recorded as
  `NOT_STARTED`; refusing it by name is the same work done once.
* **Half of what this daemon reports arrives as a `dict` from the device.**
  `state_report`, `hello_ack` and the rest are JSON off a serial link, and the
  routes assembled them into response bodies by hand. Those assemblies move
  here, where the shape they produce is the one the proto declares and a
  missing key is a decision rather than a `KeyError` at three in the morning.
"""

from .device import (
    autorun_request_from_wire,
    autorun_to_wire,
    committed_set_to_wire,
    device_capacities_to_wire,
    device_state_to_wire,
    firmware_versions_to_wire,
    line_map_view_to_wire,
    pool_counts_to_wire,
    save_settings_result_to_wire,
    scan_health_to_wire,
    serial_monitor_entry_to_wire,
    serial_monitor_window_to_wire,
    write_line_map_result_to_wire,
)
from .documents import (
    config_summaries_to_wire,
    config_summary_to_wire,
    graph_summaries_to_wire,
    graph_summary_to_wire,
    graph_validation_to_wire,
    graph_warning_to_wire,
    stored_file_to_wire,
)
from .recording import (
    manifest_to_wire,
    recording_entries_to_wire,
    recordings_to_wire,
    segment_to_wire,
)
from .rig_configuration import (
    rig_configuration_patch_from_wire,
    rig_configuration_to_wire,
    rig_configuration_update_to_wire,
)
from .session import (
    active_graph_to_wire,
    close_session_result_to_wire,
    loaded_config_result_to_wire,
    loaded_config_to_wire,
    open_session_result_to_wire,
    session_state_to_wire,
)
from .state import (
    observer_to_wire,
    observers_to_wire,
    rig_state_to_wire,
    state_frame_to_wire,
    trace_entry_to_wire,
    trace_window_to_wire,
)
from .trial import (
    configure_trial_from_wire,
    distribution_patches_from_wire,
    state_visit_to_wire,
    trial_result_to_wire,
)

__all__ = [
    "active_graph_to_wire",
    "autorun_request_from_wire",
    "autorun_to_wire",
    "close_session_result_to_wire",
    "committed_set_to_wire",
    "config_summaries_to_wire",
    "config_summary_to_wire",
    "configure_trial_from_wire",
    "device_capacities_to_wire",
    "device_state_to_wire",
    "distribution_patches_from_wire",
    "firmware_versions_to_wire",
    "graph_summaries_to_wire",
    "graph_summary_to_wire",
    "graph_validation_to_wire",
    "graph_warning_to_wire",
    "line_map_view_to_wire",
    "loaded_config_result_to_wire",
    "loaded_config_to_wire",
    "manifest_to_wire",
    "observer_to_wire",
    "observers_to_wire",
    "open_session_result_to_wire",
    "pool_counts_to_wire",
    "recording_entries_to_wire",
    "recordings_to_wire",
    "rig_configuration_patch_from_wire",
    "rig_configuration_to_wire",
    "rig_configuration_update_to_wire",
    "rig_state_to_wire",
    "save_settings_result_to_wire",
    "scan_health_to_wire",
    "segment_to_wire",
    "serial_monitor_entry_to_wire",
    "serial_monitor_window_to_wire",
    "session_state_to_wire",
    "state_frame_to_wire",
    "state_visit_to_wire",
    "stored_file_to_wire",
    "trace_entry_to_wire",
    "trace_window_to_wire",
    "trial_result_to_wire",
    "write_line_map_result_to_wire",
]
