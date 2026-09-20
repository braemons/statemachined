from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class DeviceCapacities(_message.Message):
    __slots__ = ("max_line", "max_states", "max_transitions", "max_output_actions", "max_distributions", "max_choice_options", "max_path", "max_graphs", "max_timers", "first_timer_line", "input_line_count", "output_line_count")
    MAX_LINE_FIELD_NUMBER: _ClassVar[int]
    MAX_STATES_FIELD_NUMBER: _ClassVar[int]
    MAX_TRANSITIONS_FIELD_NUMBER: _ClassVar[int]
    MAX_OUTPUT_ACTIONS_FIELD_NUMBER: _ClassVar[int]
    MAX_DISTRIBUTIONS_FIELD_NUMBER: _ClassVar[int]
    MAX_CHOICE_OPTIONS_FIELD_NUMBER: _ClassVar[int]
    MAX_PATH_FIELD_NUMBER: _ClassVar[int]
    MAX_GRAPHS_FIELD_NUMBER: _ClassVar[int]
    MAX_TIMERS_FIELD_NUMBER: _ClassVar[int]
    FIRST_TIMER_LINE_FIELD_NUMBER: _ClassVar[int]
    INPUT_LINE_COUNT_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_LINE_COUNT_FIELD_NUMBER: _ClassVar[int]
    max_line: int
    max_states: int
    max_transitions: int
    max_output_actions: int
    max_distributions: int
    max_choice_options: int
    max_path: int
    max_graphs: int
    max_timers: int
    first_timer_line: int
    input_line_count: int
    output_line_count: int
    def __init__(self, max_line: _Optional[int] = ..., max_states: _Optional[int] = ..., max_transitions: _Optional[int] = ..., max_output_actions: _Optional[int] = ..., max_distributions: _Optional[int] = ..., max_choice_options: _Optional[int] = ..., max_path: _Optional[int] = ..., max_graphs: _Optional[int] = ..., max_timers: _Optional[int] = ..., first_timer_line: _Optional[int] = ..., input_line_count: _Optional[int] = ..., output_line_count: _Optional[int] = ...) -> None: ...

class GraphPoolCounts(_message.Message):
    __slots__ = ("graphs", "states", "transitions", "output_actions", "distributions", "choice_options")
    GRAPHS_FIELD_NUMBER: _ClassVar[int]
    STATES_FIELD_NUMBER: _ClassVar[int]
    TRANSITIONS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_ACTIONS_FIELD_NUMBER: _ClassVar[int]
    DISTRIBUTIONS_FIELD_NUMBER: _ClassVar[int]
    CHOICE_OPTIONS_FIELD_NUMBER: _ClassVar[int]
    graphs: int
    states: int
    transitions: int
    output_actions: int
    distributions: int
    choice_options: int
    def __init__(self, graphs: _Optional[int] = ..., states: _Optional[int] = ..., transitions: _Optional[int] = ..., output_actions: _Optional[int] = ..., distributions: _Optional[int] = ..., choice_options: _Optional[int] = ...) -> None: ...

class CommittedGraphSet(_message.Message):
    __slots__ = ("set_version", "graph_names", "pool_usage", "pool_capacity")
    SET_VERSION_FIELD_NUMBER: _ClassVar[int]
    GRAPH_NAMES_FIELD_NUMBER: _ClassVar[int]
    POOL_USAGE_FIELD_NUMBER: _ClassVar[int]
    POOL_CAPACITY_FIELD_NUMBER: _ClassVar[int]
    set_version: int
    graph_names: _containers.RepeatedScalarFieldContainer[str]
    pool_usage: GraphPoolCounts
    pool_capacity: GraphPoolCounts
    def __init__(self, set_version: _Optional[int] = ..., graph_names: _Optional[_Iterable[str]] = ..., pool_usage: _Optional[_Union[GraphPoolCounts, _Mapping]] = ..., pool_capacity: _Optional[_Union[GraphPoolCounts, _Mapping]] = ...) -> None: ...

class LinkHealth(_message.Message):
    __slots__ = ("connection_count", "dropped_lines", "bad_lines", "last_error")
    CONNECTION_COUNT_FIELD_NUMBER: _ClassVar[int]
    DROPPED_LINES_FIELD_NUMBER: _ClassVar[int]
    BAD_LINES_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_FIELD_NUMBER: _ClassVar[int]
    connection_count: int
    dropped_lines: int
    bad_lines: int
    last_error: str
    def __init__(self, connection_count: _Optional[int] = ..., dropped_lines: _Optional[int] = ..., bad_lines: _Optional[int] = ..., last_error: _Optional[str] = ...) -> None: ...

class ScanHealth(_message.Message):
    __slots__ = ("hz", "overruns", "worst_gap", "tx_stalls")
    HZ_FIELD_NUMBER: _ClassVar[int]
    OVERRUNS_FIELD_NUMBER: _ClassVar[int]
    WORST_GAP_FIELD_NUMBER: _ClassVar[int]
    TX_STALLS_FIELD_NUMBER: _ClassVar[int]
    hz: int
    overruns: int
    worst_gap: int
    tx_stalls: int
    def __init__(self, hz: _Optional[int] = ..., overruns: _Optional[int] = ..., worst_gap: _Optional[int] = ..., tx_stalls: _Optional[int] = ...) -> None: ...

class DeviceState(_message.Message):
    __slots__ = ("connected", "target", "board", "firmware_version", "protocol_version", "measured_scan_hz", "capacities", "has_wiring", "pin_labels_came_from", "committed_set", "link", "scan", "uptime_device_microseconds")
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    BOARD_FIELD_NUMBER: _ClassVar[int]
    FIRMWARE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    MEASURED_SCAN_HZ_FIELD_NUMBER: _ClassVar[int]
    CAPACITIES_FIELD_NUMBER: _ClassVar[int]
    HAS_WIRING_FIELD_NUMBER: _ClassVar[int]
    PIN_LABELS_CAME_FROM_FIELD_NUMBER: _ClassVar[int]
    COMMITTED_SET_FIELD_NUMBER: _ClassVar[int]
    LINK_FIELD_NUMBER: _ClassVar[int]
    SCAN_FIELD_NUMBER: _ClassVar[int]
    UPTIME_DEVICE_MICROSECONDS_FIELD_NUMBER: _ClassVar[int]
    connected: bool
    target: str
    board: str
    firmware_version: str
    protocol_version: int
    measured_scan_hz: int
    capacities: DeviceCapacities
    has_wiring: bool
    pin_labels_came_from: str
    committed_set: CommittedGraphSet
    link: LinkHealth
    scan: ScanHealth
    uptime_device_microseconds: int
    def __init__(self, connected: _Optional[bool] = ..., target: _Optional[str] = ..., board: _Optional[str] = ..., firmware_version: _Optional[str] = ..., protocol_version: _Optional[int] = ..., measured_scan_hz: _Optional[int] = ..., capacities: _Optional[_Union[DeviceCapacities, _Mapping]] = ..., has_wiring: _Optional[bool] = ..., pin_labels_came_from: _Optional[str] = ..., committed_set: _Optional[_Union[CommittedGraphSet, _Mapping]] = ..., link: _Optional[_Union[LinkHealth, _Mapping]] = ..., scan: _Optional[_Union[ScanHealth, _Mapping]] = ..., uptime_device_microseconds: _Optional[int] = ...) -> None: ...

class InputLine(_message.Message):
    __slots__ = ("name", "line_index", "pin_label", "reads_active_low", "is_enabled", "debounce_milliseconds", "is_high_now")
    NAME_FIELD_NUMBER: _ClassVar[int]
    LINE_INDEX_FIELD_NUMBER: _ClassVar[int]
    PIN_LABEL_FIELD_NUMBER: _ClassVar[int]
    READS_ACTIVE_LOW_FIELD_NUMBER: _ClassVar[int]
    IS_ENABLED_FIELD_NUMBER: _ClassVar[int]
    DEBOUNCE_MILLISECONDS_FIELD_NUMBER: _ClassVar[int]
    IS_HIGH_NOW_FIELD_NUMBER: _ClassVar[int]
    name: str
    line_index: int
    pin_label: str
    reads_active_low: bool
    is_enabled: bool
    debounce_milliseconds: int
    is_high_now: bool
    def __init__(self, name: _Optional[str] = ..., line_index: _Optional[int] = ..., pin_label: _Optional[str] = ..., reads_active_low: _Optional[bool] = ..., is_enabled: _Optional[bool] = ..., debounce_milliseconds: _Optional[int] = ..., is_high_now: _Optional[bool] = ...) -> None: ...

class OutputLine(_message.Message):
    __slots__ = ("name", "line_index", "pin_label", "safe_level_is_high", "is_high_now")
    NAME_FIELD_NUMBER: _ClassVar[int]
    LINE_INDEX_FIELD_NUMBER: _ClassVar[int]
    PIN_LABEL_FIELD_NUMBER: _ClassVar[int]
    SAFE_LEVEL_IS_HIGH_FIELD_NUMBER: _ClassVar[int]
    IS_HIGH_NOW_FIELD_NUMBER: _ClassVar[int]
    name: str
    line_index: int
    pin_label: str
    safe_level_is_high: bool
    is_high_now: bool
    def __init__(self, name: _Optional[str] = ..., line_index: _Optional[int] = ..., pin_label: _Optional[str] = ..., safe_level_is_high: _Optional[bool] = ..., is_high_now: _Optional[bool] = ...) -> None: ...

class LineMapView(_message.Message):
    __slots__ = ("input_lines", "output_lines", "pin_labels_came_from", "board_input_pins", "board_output_pins")
    INPUT_LINES_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_LINES_FIELD_NUMBER: _ClassVar[int]
    PIN_LABELS_CAME_FROM_FIELD_NUMBER: _ClassVar[int]
    BOARD_INPUT_PINS_FIELD_NUMBER: _ClassVar[int]
    BOARD_OUTPUT_PINS_FIELD_NUMBER: _ClassVar[int]
    input_lines: _containers.RepeatedCompositeFieldContainer[InputLine]
    output_lines: _containers.RepeatedCompositeFieldContainer[OutputLine]
    pin_labels_came_from: str
    board_input_pins: _containers.RepeatedScalarFieldContainer[str]
    board_output_pins: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, input_lines: _Optional[_Iterable[_Union[InputLine, _Mapping]]] = ..., output_lines: _Optional[_Iterable[_Union[OutputLine, _Mapping]]] = ..., pin_labels_came_from: _Optional[str] = ..., board_input_pins: _Optional[_Iterable[str]] = ..., board_output_pins: _Optional[_Iterable[str]] = ...) -> None: ...

class SerialMonitorEntry(_message.Message):
    __slots__ = ("entry_number", "direction", "line", "recorded_host_time")
    ENTRY_NUMBER_FIELD_NUMBER: _ClassVar[int]
    DIRECTION_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    RECORDED_HOST_TIME_FIELD_NUMBER: _ClassVar[int]
    entry_number: int
    direction: str
    line: str
    recorded_host_time: str
    def __init__(self, entry_number: _Optional[int] = ..., direction: _Optional[str] = ..., line: _Optional[str] = ..., recorded_host_time: _Optional[str] = ...) -> None: ...

class SerialMonitorWindow(_message.Message):
    __slots__ = ("entries", "newest_entry_number", "oldest_entry_number_still_held", "ring_capacity", "lost_entries_before")
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    NEWEST_ENTRY_NUMBER_FIELD_NUMBER: _ClassVar[int]
    OLDEST_ENTRY_NUMBER_STILL_HELD_FIELD_NUMBER: _ClassVar[int]
    RING_CAPACITY_FIELD_NUMBER: _ClassVar[int]
    LOST_ENTRIES_BEFORE_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[SerialMonitorEntry]
    newest_entry_number: int
    oldest_entry_number_still_held: int
    ring_capacity: int
    lost_entries_before: int
    def __init__(self, entries: _Optional[_Iterable[_Union[SerialMonitorEntry, _Mapping]]] = ..., newest_entry_number: _Optional[int] = ..., oldest_entry_number_still_held: _Optional[int] = ..., ring_capacity: _Optional[int] = ..., lost_entries_before: _Optional[int] = ...) -> None: ...

class ReadSerialMonitorRequest(_message.Message):
    __slots__ = ("since_entry_number", "limit")
    SINCE_ENTRY_NUMBER_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    since_entry_number: int
    limit: int
    def __init__(self, since_entry_number: _Optional[int] = ..., limit: _Optional[int] = ...) -> None: ...

class WatchSerialMonitorRequest(_message.Message):
    __slots__ = ("since_entry_number",)
    SINCE_ENTRY_NUMBER_FIELD_NUMBER: _ClassVar[int]
    since_entry_number: int
    def __init__(self, since_entry_number: _Optional[int] = ...) -> None: ...

class WriteLineMapResult(_message.Message):
    __slots__ = ("line_map", "pushed_to_device", "saved_to_the_store", "state_machine_config")
    LINE_MAP_FIELD_NUMBER: _ClassVar[int]
    PUSHED_TO_DEVICE_FIELD_NUMBER: _ClassVar[int]
    SAVED_TO_THE_STORE_FIELD_NUMBER: _ClassVar[int]
    STATE_MACHINE_CONFIG_FIELD_NUMBER: _ClassVar[int]
    line_map: LineMapView
    pushed_to_device: bool
    saved_to_the_store: bool
    state_machine_config: str
    def __init__(self, line_map: _Optional[_Union[LineMapView, _Mapping]] = ..., pushed_to_device: _Optional[bool] = ..., saved_to_the_store: _Optional[bool] = ..., state_machine_config: _Optional[str] = ...) -> None: ...

class SaveSettingsResult(_message.Message):
    __slots__ = ("written", "write_count", "has_set", "set_version", "autorun")
    WRITTEN_FIELD_NUMBER: _ClassVar[int]
    WRITE_COUNT_FIELD_NUMBER: _ClassVar[int]
    HAS_SET_FIELD_NUMBER: _ClassVar[int]
    SET_VERSION_FIELD_NUMBER: _ClassVar[int]
    AUTORUN_FIELD_NUMBER: _ClassVar[int]
    written: bool
    write_count: int
    has_set: bool
    set_version: int
    autorun: bool
    def __init__(self, written: _Optional[bool] = ..., write_count: _Optional[int] = ..., has_set: _Optional[bool] = ..., set_version: _Optional[int] = ..., autorun: _Optional[bool] = ...) -> None: ...

class FirmwareVersions(_message.Message):
    __slots__ = ("running", "installed", "running_is_stamped", "comparable", "matches")
    RUNNING_FIELD_NUMBER: _ClassVar[int]
    INSTALLED_FIELD_NUMBER: _ClassVar[int]
    RUNNING_IS_STAMPED_FIELD_NUMBER: _ClassVar[int]
    COMPARABLE_FIELD_NUMBER: _ClassVar[int]
    MATCHES_FIELD_NUMBER: _ClassVar[int]
    running: str
    installed: str
    running_is_stamped: bool
    comparable: bool
    matches: bool
    def __init__(self, running: _Optional[str] = ..., installed: _Optional[str] = ..., running_is_stamped: _Optional[bool] = ..., comparable: _Optional[bool] = ..., matches: _Optional[bool] = ...) -> None: ...

class Autorun(_message.Message):
    __slots__ = ("enabled", "active", "graph_name", "slot", "cap_milliseconds", "seed", "next_trial_id")
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_FIELD_NUMBER: _ClassVar[int]
    GRAPH_NAME_FIELD_NUMBER: _ClassVar[int]
    SLOT_FIELD_NUMBER: _ClassVar[int]
    CAP_MILLISECONDS_FIELD_NUMBER: _ClassVar[int]
    SEED_FIELD_NUMBER: _ClassVar[int]
    NEXT_TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    enabled: bool
    active: bool
    graph_name: str
    slot: int
    cap_milliseconds: int
    seed: int
    next_trial_id: int
    def __init__(self, enabled: _Optional[bool] = ..., active: _Optional[bool] = ..., graph_name: _Optional[str] = ..., slot: _Optional[int] = ..., cap_milliseconds: _Optional[int] = ..., seed: _Optional[int] = ..., next_trial_id: _Optional[int] = ...) -> None: ...

class WriteAutorunRequest(_message.Message):
    __slots__ = ("enabled", "graph_name", "cap_milliseconds", "seed", "first_trial_id", "start_now")
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    GRAPH_NAME_FIELD_NUMBER: _ClassVar[int]
    CAP_MILLISECONDS_FIELD_NUMBER: _ClassVar[int]
    SEED_FIELD_NUMBER: _ClassVar[int]
    FIRST_TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    START_NOW_FIELD_NUMBER: _ClassVar[int]
    enabled: bool
    graph_name: str
    cap_milliseconds: int
    seed: int
    first_trial_id: int
    start_now: bool
    def __init__(self, enabled: _Optional[bool] = ..., graph_name: _Optional[str] = ..., cap_milliseconds: _Optional[int] = ..., seed: _Optional[int] = ..., first_trial_id: _Optional[int] = ..., start_now: _Optional[bool] = ...) -> None: ...
