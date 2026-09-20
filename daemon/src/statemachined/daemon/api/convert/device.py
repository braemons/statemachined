# SPDX-License-Identifier: LGPL-3.0-or-later
"""The board, and the lines it drives.

**Most of this reads a `dict`**, and that is the honest shape of the problem:
`state_report` and `hello_ack` are JSON off a serial link, so what arrives is
whatever that firmware sends. The routes used to assemble response bodies from
them field by field; those assemblies live here now, where the shape they
produce is the one the proto declares.

The rule for a missing key is written once, in `_int` and friends below, and it
is *not* "raise": a board older than this daemon legitimately does not send
`tx_stalls`, and refusing to describe a rig because of it would be the version
gate `contracts/INTERACTIONS.md` §11 forbids between daemons, applied to
firmware. A missing number reads as zero and a missing answer reads as absent,
which are different fields for a reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from statemachined._proto.statemachined.v1 import device_pb2
from statemachined.model.line_map import LineMap

if TYPE_CHECKING:  # pragma: no cover
    # Under `TYPE_CHECKING` because importing it for real here inverts an
    # existing cycle: `graph_set_compiler` imports `device`, which imports
    # `graph_set_compiler` back, and it resolves only when `device` is reached
    # first. Nothing in this module needs the class at runtime — it is an
    # annotation and a duck — so the cycle is not this seam's to untangle.
    from statemachined.graph_set_compiler import DeviceCapabilities


def _int(source: dict | None, key: str, default: int = 0) -> int:
    value = (source or {}).get(key)
    return int(value) if isinstance(value, int | float) else default


def _text(source: dict | None, key: str, default: str = "") -> str:
    value = (source or {}).get(key)
    return str(value) if value is not None else default


def scan_health_to_wire(scan: dict | None) -> device_pb2.ScanHealth:
    """The board's own timing counters.

    **The honest half of the timing claim.** A board that quietly misses scans
    looks exactly like one that is fine, so these are reported whether or not
    anybody asked, and `overruns` is the number that makes `hz` mean something.
    """
    return device_pb2.ScanHealth(
        hz=_int(scan, "hz"),
        overruns=_int(scan, "overruns"),
        worst_gap=_int(scan, "worst_gap"),
        tx_stalls=_int(scan, "tx_stalls"),
    )


def device_capacities_to_wire(
    capabilities: DeviceCapabilities | None,
) -> device_pb2.DeviceCapacities | None:
    """What this board can hold, or nothing when none has said.

    `None` rather than a zeroed message: a board with `max_states = 0` and a
    board that has not greeted are different situations, and only one of them
    means a graph will be refused.
    """
    if capabilities is None:
        return None
    return device_pb2.DeviceCapacities(
        max_line=capabilities.max_line,
        max_states=capabilities.max_states,
        max_transitions=capabilities.max_transitions,
        max_output_actions=capabilities.max_output_actions,
        max_distributions=capabilities.max_distributions,
        max_choice_options=capabilities.max_choice_options,
        max_path=capabilities.max_path,
        max_graphs=capabilities.max_graphs,
        max_timers=capabilities.max_timers,
        first_timer_line=capabilities.first_timer_line,
        input_line_count=capabilities.input_line_count,
        output_line_count=capabilities.output_line_count,
    )


#: The pools a board has, in the order a panel reads them. Written out rather
#: than taken from the dict's keys so that a pool the compiler adds and this
#: message does not know about is a `KeyError` here — at the seam, where it is
#: still cheap — instead of a column that silently stops being shown.
POOL_NAMES = (
    "graphs",
    "states",
    "transitions",
    "output_actions",
    "distributions",
    "choice_options",
)


def pool_counts_to_wire(counts: dict[str, int]) -> device_pb2.GraphPoolCounts:
    """What a set costs, or what a board holds. Six numbers, not one.

    `graph_set_compiler` measures six pools and they fill independently: a set
    can be two states short of the limit with room for forty more transitions.
    This was one `int32` in the first cut of the interface, which made every
    rpc carrying it raise `'dict' object cannot be interpreted as an integer` —
    found by driving a session against a real board.
    """
    return device_pb2.GraphPoolCounts(**{name: int(counts[name]) for name in POOL_NAMES})


def committed_set_to_wire(committed: Any | None) -> device_pb2.CommittedGraphSet | None:
    """The graph set on the board, by the names it was compiled from."""
    if committed is None:
        return None
    return device_pb2.CommittedGraphSet(
        set_version=committed.set_version,
        graph_names=[graph.name for graph in committed.graphs_by_slot],
        pool_usage=pool_counts_to_wire(committed.pool_usage),
        pool_capacity=pool_counts_to_wire(committed.pool_capacity),
    )


def device_state_to_wire(
    *,
    connected: bool,
    target: str,
    hello_ack: dict | None,
    state_report: dict | None,
    capabilities: DeviceCapabilities | None,
    committed: Any | None,
    pin_labels_came_from: str,
    connection_count: int,
    last_error: str,
) -> device_pb2.DeviceState:
    """Everything about the attachment, in one message.

    Assembled from three sources because that is what it is: the greeting says
    what the board *is*, the state report says what it is *doing*, and this
    daemon holds the rest. A caller should not have to make three calls and
    join them.
    """
    message = device_pb2.DeviceState(
        connected=connected,
        target=target,
        board=_text(hello_ack, "board"),
        firmware_version=_text(hello_ack, "fw"),
        protocol_version=_int(hello_ack, "proto"),
        measured_scan_hz=_int(hello_ack, "scan_hz"),
        pin_labels_came_from=pin_labels_came_from,
        link=device_pb2.LinkHealth(
            connection_count=connection_count,
            dropped_lines=_int(state_report, "dropped_lines"),
            bad_lines=_int(state_report, "bad_lines"),
            last_error=last_error,
        ),
        scan=scan_health_to_wire((state_report or {}).get("scan")),
        uptime_device_microseconds=_int(state_report, "up_us"),
    )

    capacities = device_capacities_to_wire(capabilities)
    if capacities is not None:
        message.capacities.CopyFrom(capacities)

    committed_set = committed_set_to_wire(committed)
    if committed_set is not None:
        message.committed_set.CopyFrom(committed_set)

    # Three answers, not two: the board said it has a wiring table, the board
    # said it has none, or nothing has said. The third is why this is
    # `optional` and why the routes' `.get(..., .get(...))` chain became one
    # decision here.
    has_wiring = (state_report or {}).get("has_wiring", (hello_ack or {}).get("has_wiring"))
    if has_wiring is not None:
        message.has_wiring = bool(has_wiring)

    return message


def line_map_view_to_wire(
    line_map: LineMap,
    *,
    input_word: int | None,
    output_word: int | None,
    pin_labels_came_from: str,
    board_input_pins: list[str],
    board_output_pins: list[str],
) -> device_pb2.LineMapView:
    """The map, resolved, with what each line reads right now.

    `is_high_now` is absent rather than false when nothing is attached. There
    is no read-back path from a pin, so the words are the device's own account
    — and the difference between "low" and "nobody asked the board" is the
    difference between a wiring fault and a disconnected cable.
    """
    view = device_pb2.LineMapView(
        pin_labels_came_from=pin_labels_came_from,
        board_input_pins=board_input_pins,
        board_output_pins=board_output_pins,
    )
    for definition in line_map.input_lines:
        line = view.input_lines.add(
            name=definition.name,
            pin_label=definition.pin_label,
            reads_active_low=definition.reads_active_low,
            is_enabled=definition.is_enabled,
            debounce_milliseconds=definition.debounce_milliseconds,
        )
        if definition.line_index is not None:
            line.line_index = definition.line_index
            if input_word is not None:
                line.is_high_now = bool(input_word >> definition.line_index & 1)
    for definition in line_map.output_lines:
        line = view.output_lines.add(
            name=definition.name,
            pin_label=definition.pin_label,
            safe_level_is_high=definition.safe_level_is_high,
        )
        if definition.line_index is not None:
            line.line_index = definition.line_index
            if output_word is not None:
                line.is_high_now = bool(output_word >> definition.line_index & 1)
    return view


def serial_monitor_entry_to_wire(entry: dict) -> device_pb2.SerialMonitorEntry:
    """One line of NDJSON in or out of the port.

    A line of *text*. `DeviceLineMonitor` and `LineMap` both say "line" and
    mean different things; the wire says `Serial` where it means the port.
    """
    return device_pb2.SerialMonitorEntry(
        entry_number=_int(entry, "entry_number"),
        direction=_text(entry, "direction"),
        line=_text(entry, "line"),
        recorded_host_time=_text(entry, "recorded_host_time"),
    )


def serial_monitor_window_to_wire(
    entries: list[dict],
    *,
    newest_entry_number: int,
    oldest_entry_number_still_held: int,
    ring_capacity: int,
    lost_entries_before: int | None,
) -> device_pb2.SerialMonitorWindow:
    window = device_pb2.SerialMonitorWindow(
        entries=[serial_monitor_entry_to_wire(entry) for entry in entries],
        newest_entry_number=newest_entry_number,
        oldest_entry_number_still_held=oldest_entry_number_still_held,
        ring_capacity=ring_capacity,
    )
    if lost_entries_before is not None:
        window.lost_entries_before = lost_entries_before
    return window


def firmware_versions_to_wire(comparison: dict) -> device_pb2.FirmwareVersions:
    """What the board runs against what this package ships.

    `running_is_stamped` and `comparable` are kept apart from `matches`
    deliberately: an unstamped local build makes the comparison *meaningless*
    rather than false, and a UI that showed a red cross for it would be lying
    about a board nobody can compare.
    """
    return device_pb2.FirmwareVersions(
        running=_text(comparison, "running"),
        installed=_text(comparison, "installed"),
        running_is_stamped=bool(comparison.get("running_is_stamped")),
        comparable=bool(comparison.get("comparable")),
        matches=bool(comparison.get("matches")),
    )


def autorun_to_wire(reply: dict) -> device_pb2.Autorun:
    """What the board would do on its own.

    `enabled` and `active` are not the same fact (`protocol.md` §3.7): a board
    can be configured to arm its own trials and not be doing so right now.
    """
    return device_pb2.Autorun(
        enabled=bool(reply.get("enabled")),
        active=bool(reply.get("active")),
        graph_name=_text(reply, "graph"),
        slot=_int(reply, "slot"),
        cap_milliseconds=_int(reply, "cap_ms"),
        seed=_int(reply, "seed"),
        next_trial_id=_int(reply, "next_trial_id"),
    )


def autorun_request_from_wire(request: device_pb2.WriteAutorunRequest) -> dict:
    """Only what was set, for the same reason every patch works that way.

    Left out, whatever the board already holds stands — which for a board
    restored from its own storage is the seed that makes an unattended session
    replay. A zero seed is a seed.
    """
    arguments: dict[str, Any] = {"cap_milliseconds": request.cap_milliseconds}
    if request.HasField("graph_name"):
        arguments["graph_name"] = request.graph_name
    if request.HasField("seed"):
        arguments["seed"] = request.seed
    if request.HasField("first_trial_id"):
        arguments["first_trial_id"] = request.first_trial_id
    if request.HasField("start_now"):
        arguments["start_now"] = request.start_now
    return arguments


def save_settings_result_to_wire(saved: dict[str, Any]) -> device_pb2.SaveSettingsResult:
    """What the board said about writing its own flash.

    Not "it worked". `write_count` is a wear budget — about 100,000 erase
    cycles on the reference board — and `written: false` says the settings
    were already there and no cycle was spent, which is what makes a save
    button safe to press twice.
    """
    return device_pb2.SaveSettingsResult(
        written=bool(saved.get("written")),
        write_count=int(saved.get("write_count", 0)),
        has_set=bool(saved.get("has_set")),
        set_version=int(saved.get("set_version") or 0),
        autorun=bool(saved.get("autorun")),
    )


def write_line_map_result_to_wire(
    view: device_pb2.LineMapView,
    *,
    pushed_to_device: bool,
    state_machine_config: str,
) -> device_pb2.WriteLineMapResult:
    """Where the map landed, and where it did not.

    `saved_to_the_store` is always false and is named rather than omitted: a UI
    has to be able to tell somebody their edit is one restart away from being
    lost.
    """
    result = device_pb2.WriteLineMapResult(
        pushed_to_device=pushed_to_device,
        saved_to_the_store=False,
        state_machine_config=state_machine_config,
    )
    result.line_map.CopyFrom(view)
    return result
