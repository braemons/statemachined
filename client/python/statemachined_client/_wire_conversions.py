# SPDX-License-Identifier: LGPL-3.0-or-later
"""The seam: generated types on one side, `api_types` on the other.

The only module in this package that imports the generated code, which is what
makes "no protobuf type crosses the edge" a fact about the imports rather than
a promise in a docstring.

Named `X_from_wire` and `X_to_wire`, in that direction and nothing else, so the
direction of a call is readable rather than looked up — the same rule the
daemon's own `api/convert/` keeps.

**The enum tables are exhaustive and are written out.** Deriving them by
stripping a prefix would be shorter and would silently mis-translate the first
value that does not follow the pattern; a table refuses a value it does not
know, loudly, at the seam, which is where a mistranslation is still cheap.

**Absent is not zero, and this module is where the difference is kept.**
protobuf's `optional` and its message fields both answer `HasField`, and every
`| None` in `api_types` comes from one of those. A conversion that read
`message.trial_id` unconditionally would turn "no trial" into "trial 0", which
is a trial.
"""

from __future__ import annotations

import datetime as dt

# protobuf ships no stubs for its own well-known types that a checker can
# follow, so `Struct` comes back as unknown. It is exercised by the seam tests,
# which is the check that runs the code.
from google.protobuf import json_format, struct_pb2

from ._proto.braemons.v1 import trial_outcome_pb2
from ._proto.statemachined.v1 import (
    device_pb2,
    documents_pb2,
    recording_pb2,
    rig_configuration_pb2,
    service_pb2,
    session_pb2,
    state_pb2,
    trial_pb2,
)
from .api_types import (
    Autorun,
    CancelTrialResult,
    CommittedGraphSet,
    ConfigureTrialResult,
    DeviceCapacities,
    DeviceState,
    DistributionPatch,
    FirmwareVersions,
    GraphSummary,
    GraphValidation,
    Health,
    InputLine,
    LineMapView,
    LinkHealth,
    LoadedConfigResult,
    LoadedStateMachineConfig,
    Observer,
    Observers,
    OpenSessionResult,
    OutputLine,
    RecordingEntries,
    RecordingManifest,
    Recordings,
    RecordingSegment,
    RigConfiguration,
    RigConfigurationPatch,
    RigConfigurationUpdate,
    RigState,
    ScanHealth,
    SerialMonitorEntry,
    SerialMonitorWindow,
    SessionState,
    StartTrialResult,
    StateMachineConfigSummaries,
    StateMachineConfigSummary,
    StateVisit,
    StoredFile,
    TraceEntry,
    TraceWindow,
    TrialCancelReason,
    TrialOutcome,
    TrialResult,
    WriteLineMapResult,
)

# -- the small helpers ----------------------------------------------------------
#
# Three of them, and each exists because the alternative is the same mistake
# repeated at fifty call sites.


def _maybe(message, name: str):
    """A field that is `optional`, or a message field, as itself or `None`.

    `HasField` is the only honest way to ask. Reading the attribute gives zero,
    an empty string or a default-constructed message for something the daemon
    never set, and every one of those is a value a caller would act on.
    """
    return getattr(message, name) if message.HasField(name) else None


def _host_time(text: str) -> dt.datetime | None:
    """An ISO-8601 host time, as a `datetime`.

    `None` for an empty string and for anything that will not parse. **Not an
    exception**: these stamps are written by the trace ring and read back from
    `.jsonl` files that may have been written by an older daemon, and a trace
    you can read with one odd timestamp in it beats a trace you cannot read.

    `Z` is rewritten because `fromisoformat` did not accept it before 3.11 and
    this package supports 3.11 — where it does, but the rewrite costs nothing
    and removes the question.
    """
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _plain(value):
    """One `google.protobuf.Struct` value, as the Python it came from.

    Struct has one number type and it is a double, so an entry number that was
    an `int` in the daemon's dict arrives as `4.0`. An integral float is turned
    back into an `int` here, because the alternative is every consumer of a
    trace payload writing `int(payload["state_index"])` and one of them
    forgetting.

    The range where this is lossy is above 2**53, which no field in a trace
    payload reaches: the largest are device microseconds, and the board would
    have to run for 285 years.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _struct(message: struct_pb2.Struct) -> dict:  # ty: ignore[unresolved-attribute]
    return _plain(json_format.MessageToDict(message))


# -- enums ----------------------------------------------------------------------
#
# `braemons.v1.TrialOutcome` needs no table: it is shared with triald and with
# the firmware, its numbers *are* the contract, and its value names carry no
# prefix. The cast is the whole conversion, and an outcome this client has not
# heard of would be a `ValueError` — which is right, because an outcome code is
# the one thing here that nothing may guess at.

_CANCEL_REASON_TO_WIRE = {
    TrialCancelReason.NONE: trial_pb2.TRIAL_CANCEL_REASON_NONE,
    TrialCancelReason.HOST: trial_pb2.TRIAL_CANCEL_REASON_HOST,
    TrialCancelReason.LINK_LOST: trial_pb2.TRIAL_CANCEL_REASON_LINK_LOST,
    TrialCancelReason.ABORT_LINE: trial_pb2.TRIAL_CANCEL_REASON_ABORT_LINE,
    TrialCancelReason.TRIAL_TIMEOUT: trial_pb2.TRIAL_CANCEL_REASON_TRIAL_TIMEOUT,
}
_CANCEL_REASON_FROM_WIRE = {value: key for key, value in _CANCEL_REASON_TO_WIRE.items()}


def trial_outcome_from_wire(value: int) -> TrialOutcome:
    return TrialOutcome(value)


def trial_outcome_to_wire(outcome: TrialOutcome) -> int:
    return trial_outcome_pb2.TrialOutcome.Value(outcome.name)


def cancel_reason_from_wire(value: int) -> TrialCancelReason:
    # The keys are the generated enum's members, which are `int` at runtime and
    # a distinct type to a checker; the wire hands us a plain `int`.
    return _CANCEL_REASON_FROM_WIRE[value]  # ty: ignore[invalid-argument-type]


def cancel_reason_to_wire(reason: TrialCancelReason) -> int:
    return _CANCEL_REASON_TO_WIRE[reason]


# -- the device -----------------------------------------------------------------


def capacities_from_wire(message: device_pb2.DeviceCapacities) -> DeviceCapacities:
    return DeviceCapacities(
        max_line=message.max_line,
        max_states=message.max_states,
        max_transitions=message.max_transitions,
        max_output_actions=message.max_output_actions,
        max_distributions=message.max_distributions,
        max_choice_options=message.max_choice_options,
        max_path=message.max_path,
        max_graphs=message.max_graphs,
        max_timers=message.max_timers,
        first_timer_line=message.first_timer_line,
        input_line_count=message.input_line_count,
        output_line_count=message.output_line_count,
    )


def committed_set_from_wire(message: device_pb2.CommittedGraphSet) -> CommittedGraphSet:
    return CommittedGraphSet(
        set_version=message.set_version,
        graph_names=list(message.graph_names),
        pool_usage=message.pool_usage,
        pool_capacity=message.pool_capacity,
    )


def link_health_from_wire(message: device_pb2.LinkHealth) -> LinkHealth:
    return LinkHealth(
        connection_count=message.connection_count,
        dropped_lines=message.dropped_lines,
        bad_lines=message.bad_lines,
        last_error=message.last_error,
    )


def scan_health_from_wire(message: device_pb2.ScanHealth) -> ScanHealth:
    return ScanHealth(
        hz=message.hz,
        overruns=message.overruns,
        worst_gap=message.worst_gap,
        tx_stalls=message.tx_stalls,
    )


def device_state_from_wire(message: device_pb2.DeviceState) -> DeviceState:
    capacities = _maybe(message, "capacities")
    committed = _maybe(message, "committed_set")
    link = _maybe(message, "link")
    scan = _maybe(message, "scan")
    return DeviceState(
        connected=message.connected,
        target=message.target,
        board=message.board,
        firmware_version=message.firmware_version,
        protocol_version=message.protocol_version,
        measured_scan_hz=message.measured_scan_hz,
        capacities=capacities and capacities_from_wire(capacities),
        has_wiring=_maybe(message, "has_wiring"),
        pin_labels_came_from=message.pin_labels_came_from,
        committed_set=committed and committed_set_from_wire(committed),
        link=link and link_health_from_wire(link),
        scan=scan and scan_health_from_wire(scan),
        uptime_device_microseconds=message.uptime_device_microseconds,
    )


def input_line_from_wire(message: device_pb2.InputLine) -> InputLine:
    return InputLine(
        name=message.name,
        line_index=_maybe(message, "line_index"),
        pin_label=message.pin_label,
        reads_active_low=message.reads_active_low,
        is_enabled=message.is_enabled,
        is_high_now=_maybe(message, "is_high_now"),
    )


def output_line_from_wire(message: device_pb2.OutputLine) -> OutputLine:
    return OutputLine(
        name=message.name,
        line_index=_maybe(message, "line_index"),
        pin_label=message.pin_label,
        safe_level_is_high=message.safe_level_is_high,
        is_high_now=_maybe(message, "is_high_now"),
    )


def line_map_from_wire(message: device_pb2.LineMapView) -> LineMapView:
    return LineMapView(
        input_lines=[input_line_from_wire(line) for line in message.input_lines],
        output_lines=[output_line_from_wire(line) for line in message.output_lines],
        pin_labels_came_from=message.pin_labels_came_from,
        board_input_pins=list(message.board_input_pins),
        board_output_pins=list(message.board_output_pins),
    )


def write_line_map_result_from_wire(
    message: device_pb2.WriteLineMapResult,
) -> WriteLineMapResult:
    line_map = _maybe(message, "line_map")
    return WriteLineMapResult(
        line_map=line_map and line_map_from_wire(line_map),
        pushed_to_device=message.pushed_to_device,
        saved_to_the_store=message.saved_to_the_store,
        state_machine_config=message.state_machine_config,
    )


def serial_monitor_entry_from_wire(
    message: device_pb2.SerialMonitorEntry,
) -> SerialMonitorEntry:
    return SerialMonitorEntry(
        entry_number=message.entry_number,
        direction=message.direction,
        line=message.line,
        recorded_host_time=_host_time(message.recorded_host_time),
    )


def serial_monitor_window_from_wire(
    message: device_pb2.SerialMonitorWindow,
) -> SerialMonitorWindow:
    return SerialMonitorWindow(
        entries=[serial_monitor_entry_from_wire(entry) for entry in message.entries],
        newest_entry_number=message.newest_entry_number,
        oldest_entry_number_still_held=message.oldest_entry_number_still_held,
        ring_capacity=message.ring_capacity,
        lost_entries_before=_maybe(message, "lost_entries_before"),
    )


def firmware_from_wire(message: device_pb2.FirmwareVersions) -> FirmwareVersions:
    return FirmwareVersions(
        running=message.running,
        installed=message.installed,
        running_is_stamped=message.running_is_stamped,
        comparable=message.comparable,
        matches=message.matches,
    )


def autorun_from_wire(message: device_pb2.Autorun) -> Autorun:
    return Autorun(
        enabled=message.enabled,
        active=message.active,
        graph_name=message.graph_name,
        slot=message.slot,
        cap_milliseconds=message.cap_milliseconds,
        seed=message.seed,
        next_trial_id=message.next_trial_id,
    )


# -- the documents --------------------------------------------------------------


def stored_file_from_wire(message: documents_pb2.StoredFile) -> StoredFile:
    return StoredFile(name=message.name, text=message.text)


def graph_summary_from_wire(message: documents_pb2.GraphSummary) -> GraphSummary:
    return GraphSummary(
        name=message.name,
        readable=message.readable,
        detail=message.detail,
        state_count=message.state_count,
        entry=message.entry,
    )


def graph_validation_from_wire(message: documents_pb2.GraphValidation) -> GraphValidation:
    return GraphValidation(
        valid=message.valid,
        detail=message.detail,
        pool_usage=message.pool_usage,
        pool_capacity=message.pool_capacity,
        warnings=list(message.warnings),
    )


def config_summary_from_wire(
    message: documents_pb2.StateMachineConfigSummary,
) -> StateMachineConfigSummary:
    return StateMachineConfigSummary(
        name=message.name,
        readable=message.readable,
        detail=message.detail,
        description=message.description,
        board=message.board,
        graph_names=list(message.graph_names),
        input_line_count=message.input_line_count,
        output_line_count=message.output_line_count,
    )


def config_summaries_from_wire(
    message: documents_pb2.StateMachineConfigSummaries,
) -> StateMachineConfigSummaries:
    return StateMachineConfigSummaries(
        configs=[config_summary_from_wire(item) for item in message.configs],
        loaded=message.loaded,
    )


# -- the session ----------------------------------------------------------------


def loaded_config_from_wire(
    message: session_pb2.LoadedStateMachineConfig,
) -> LoadedStateMachineConfig:
    return LoadedStateMachineConfig(
        name=message.name,
        description=message.description,
        board=message.board,
        graph_names=list(message.graph_names),
        still_in_the_store=message.still_in_the_store,
    )


def session_state_from_wire(message: session_pb2.SessionState) -> SessionState:
    config = _maybe(message, "state_machine_config")
    committed = _maybe(message, "committed_set")
    return SessionState(
        state_machine_config=config and loaded_config_from_wire(config),
        committed_set=committed and committed_set_from_wire(committed),
        session_open=message.session_open,
        opened_at_unix_seconds=_maybe(message, "opened_at_unix_seconds"),
        open_seconds=_maybe(message, "open_seconds"),
        active_graph=message.active_graph,
        stored_config_names=list(message.stored_config_names),
    )


def open_session_result_from_wire(
    message: session_pb2.OpenSessionResult,
) -> OpenSessionResult:
    return OpenSessionResult(
        state_machine_config=message.state_machine_config,
        set_version=message.set_version,
        slots=dict(message.slots),
        pool_usage=message.pool_usage,
        pool_capacity=message.pool_capacity,
        elapsed_milliseconds=message.elapsed_milliseconds,
    )


def loaded_config_result_from_wire(
    message: session_pb2.LoadedConfigResult,
) -> LoadedConfigResult:
    line_map = _maybe(message, "line_map")
    return LoadedConfigResult(
        loaded=message.loaded,
        wiring_pushed=message.wiring_pushed,
        line_map=line_map and line_map_from_wire(line_map),
        graph_names=list(message.graph_names),
    )


# -- trials ---------------------------------------------------------------------


def distribution_patch_to_wire(patch: DistributionPatch) -> trial_pb2.DistributionPatch:
    """The one type that travels outward, and the only place `None` means absent.

    A patch sets the fields it names and leaves the rest to the graph, so an
    unset field must not arrive as zero — `minimum_ms=0` is a distribution that
    can draw nothing, which is a different experiment.
    """
    message = trial_pb2.DistributionPatch(name=patch.name)
    for name in ("minimum_ms", "maximum_ms", "mean_ms", "duration_ms"):
        value = getattr(patch, name)
        if value is not None:
            setattr(message, name, value)
    return message


def configure_result_from_wire(
    message: trial_pb2.ConfigureTrialResult,
) -> ConfigureTrialResult:
    return ConfigureTrialResult(
        trial_id=message.trial_id,
        graph=message.graph,
        set_version=message.set_version,
        graph_index=message.graph_index,
        elapsed_milliseconds=message.elapsed_milliseconds,
    )


def start_result_from_wire(message: trial_pb2.StartTrialResult) -> StartTrialResult:
    return StartTrialResult(
        trial_id=message.trial_id,
        started_device_microseconds=message.started_device_microseconds,
    )


def cancel_result_from_wire(message: service_pb2.CancelTrialResult) -> CancelTrialResult:
    return CancelTrialResult(
        trial_id=message.trial_id,
        cancelled=message.cancelled,
        outcome_code=message.outcome_code,
    )


def state_visit_from_wire(message: trial_pb2.StateVisit) -> StateVisit:
    return StateVisit(
        state_name=message.state_name,
        exit_cause=message.exit_cause,
        fired_transition_position=_maybe(message, "fired_transition_position"),
        fired_transition_target_state_name=_maybe(
            message, "fired_transition_target_state_name"
        ),
        drawn_duration_ms=message.drawn_duration_ms,
        entered_device_microseconds=message.entered_device_microseconds,
        measured_duration_microseconds=message.measured_duration_microseconds,
    )


def trial_result_from_wire(message: trial_pb2.TrialResult) -> TrialResult:
    return TrialResult(
        trial_id=message.trial_id,
        outcome=trial_outcome_from_wire(message.outcome),
        cancel_reason=cancel_reason_from_wire(message.cancel_reason),
        total_duration_microseconds=message.total_duration_microseconds,
        visits=[state_visit_from_wire(visit) for visit in message.visits],
        path_was_truncated=message.path_was_truncated,
        first_visit_sequence_number=message.first_visit_sequence_number,
        total_visit_count=message.total_visit_count,
    )


# -- state and the trace --------------------------------------------------------


def rig_state_from_wire(message: state_pb2.RigState) -> RigState:
    scan = _maybe(message, "scan")
    return RigState(
        connected=message.connected,
        link_state=message.link_state,
        running=message.running,
        trial_id=_maybe(message, "trial_id"),
        graph=message.graph,
        state_name=_maybe(message, "state_name"),
        state_index=_maybe(message, "state_index"),
        input_word=_maybe(message, "input_word"),
        output_word=_maybe(message, "output_word"),
        scan=scan and scan_health_from_wire(scan),
        newest_trace_entry_number=message.newest_trace_entry_number,
    )


def trace_entry_from_wire(message: state_pb2.TraceEntry) -> TraceEntry:
    return TraceEntry(
        entry_number=message.entry_number,
        kind=message.kind,
        recorded_host_time=_host_time(message.recorded_host_time),
        trial_id=_maybe(message, "trial_id"),
        payload=_struct(message.payload),
    )


def trace_window_from_wire(message: state_pb2.TraceWindow) -> TraceWindow:
    return TraceWindow(
        entries=[trace_entry_from_wire(entry) for entry in message.entries],
        newest_entry_number=message.newest_entry_number,
        oldest_entry_number_still_held=message.oldest_entry_number_still_held,
        ring_capacity=message.ring_capacity,
        lost_entries_before=_maybe(message, "lost_entries_before"),
    )


def observer_from_wire(message: state_pb2.Observer) -> Observer:
    return Observer(
        observer_id=message.observer_id,
        name=message.name,
        stream=message.stream,
        address=message.address,
        connected_at_unix_seconds=message.connected_at_unix_seconds,
        connected_seconds=message.connected_seconds,
        delivered=message.delivered,
        fell_behind=message.fell_behind,
    )


def observers_from_wire(message: state_pb2.Observers) -> Observers:
    return Observers(
        observers=[observer_from_wire(item) for item in message.observers],
        count=message.count,
    )


# -- recordings -----------------------------------------------------------------


def recording_segment_from_wire(
    message: recording_pb2.RecordingSegment,
) -> RecordingSegment:
    return RecordingSegment(
        from_entry_number=_maybe(message, "from_entry_number"),
        to_entry_number=_maybe(message, "to_entry_number"),
        started_host_time=_host_time(message.started_host_time),
        ended_host_time=_host_time(message.ended_host_time),
        entry_count=message.entry_count,
    )


def recording_manifest_from_wire(
    message: recording_pb2.RecordingManifest,
) -> RecordingManifest:
    return RecordingManifest(
        name=message.name,
        description=message.description,
        state_machine_config=message.state_machine_config,
        created_unix_seconds=message.created_unix_seconds,
        created_host_time=_host_time(message.created_host_time),
        state=message.state,
        segments=[recording_segment_from_wire(item) for item in message.segments],
        entry_count=message.entry_count,
        kind_counts=dict(message.kind_counts),
        unreadable=message.unreadable,
    )


def recordings_from_wire(message: recording_pb2.Recordings) -> Recordings:
    active = _maybe(message, "active")
    return Recordings(
        active=active and recording_manifest_from_wire(active),
        recordings=[recording_manifest_from_wire(item) for item in message.recordings],
    )


def recording_entries_from_wire(
    message: recording_pb2.RecordingEntries,
) -> RecordingEntries:
    return RecordingEntries(
        name=message.name,
        offset=message.offset,
        entries=[trace_entry_from_wire(entry) for entry in message.entries],
        entry_count=message.entry_count,
        segments=[recording_segment_from_wire(item) for item in message.segments],
    )


# -- the rig configuration ------------------------------------------------------


def rig_configuration_from_wire(
    message: rig_configuration_pb2.RigConfiguration,
) -> RigConfiguration:
    return RigConfiguration(
        device_target=message.device_target,
        device_baud=message.device_baud,
        device_timeout_seconds=message.device_timeout_seconds,
        expected_board=message.expected_board,
        connect_on_startup=message.connect_on_startup,
        startup_state_machine_config=message.startup_state_machine_config,
        graph_mode=message.graph_mode,
        trace_ring_entries=message.trace_ring_entries,
        heartbeat_seconds=message.heartbeat_seconds,
        trace_directory=message.trace_directory,
        graph_store_directory=message.graph_store_directory,
        recording_directory=message.recording_directory,
        state_machine_config_directory=message.state_machine_config_directory,
    )


def rig_configuration_patch_to_wire(
    patch: RigConfigurationPatch,
) -> rig_configuration_pb2.RigConfigurationPatch:
    """`None` means "leave it", which is why every field here is `optional`.

    An empty string is a value: `expected_board=""` is "accept any board", and
    a patch that could not tell it from "do not change the expected board"
    would make that setting unreachable over the API.
    """
    message = rig_configuration_pb2.RigConfigurationPatch()
    for name in (
        "device_target",
        "device_baud",
        "expected_board",
        "graph_mode",
        "startup_state_machine_config",
    ):
        value = getattr(patch, name)
        if value is not None:
            setattr(message, name, value)
    return message


def rig_configuration_update_from_wire(
    message: rig_configuration_pb2.RigConfigurationUpdate,
) -> RigConfigurationUpdate:
    configuration = _maybe(message, "configuration")
    return RigConfigurationUpdate(
        configuration=configuration and rig_configuration_from_wire(configuration),
        reconnected=message.reconnected,
        until_restart=message.until_restart,
    )


# -- health ---------------------------------------------------------------------


def health_from_wire(message: service_pb2.Health) -> Health:
    return Health(ok=message.ok, device_connected=message.device_connected)
