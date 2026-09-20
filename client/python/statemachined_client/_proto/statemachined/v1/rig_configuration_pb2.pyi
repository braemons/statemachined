from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RigConfiguration(_message.Message):
    __slots__ = ("device_target", "device_baud", "device_timeout_seconds", "expected_board", "connect_on_startup", "startup_state_machine_config", "graph_mode", "trace_ring_entries", "heartbeat_seconds", "trace_directory", "graph_store_directory", "recording_directory", "state_machine_config_directory")
    DEVICE_TARGET_FIELD_NUMBER: _ClassVar[int]
    DEVICE_BAUD_FIELD_NUMBER: _ClassVar[int]
    DEVICE_TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_BOARD_FIELD_NUMBER: _ClassVar[int]
    CONNECT_ON_STARTUP_FIELD_NUMBER: _ClassVar[int]
    STARTUP_STATE_MACHINE_CONFIG_FIELD_NUMBER: _ClassVar[int]
    GRAPH_MODE_FIELD_NUMBER: _ClassVar[int]
    TRACE_RING_ENTRIES_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    TRACE_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    GRAPH_STORE_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    RECORDING_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    STATE_MACHINE_CONFIG_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    device_target: str
    device_baud: int
    device_timeout_seconds: float
    expected_board: str
    connect_on_startup: bool
    startup_state_machine_config: str
    graph_mode: str
    trace_ring_entries: int
    heartbeat_seconds: float
    trace_directory: str
    graph_store_directory: str
    recording_directory: str
    state_machine_config_directory: str
    def __init__(self, device_target: _Optional[str] = ..., device_baud: _Optional[int] = ..., device_timeout_seconds: _Optional[float] = ..., expected_board: _Optional[str] = ..., connect_on_startup: _Optional[bool] = ..., startup_state_machine_config: _Optional[str] = ..., graph_mode: _Optional[str] = ..., trace_ring_entries: _Optional[int] = ..., heartbeat_seconds: _Optional[float] = ..., trace_directory: _Optional[str] = ..., graph_store_directory: _Optional[str] = ..., recording_directory: _Optional[str] = ..., state_machine_config_directory: _Optional[str] = ...) -> None: ...

class RigConfigurationPatch(_message.Message):
    __slots__ = ("device_target", "device_baud", "expected_board", "graph_mode", "startup_state_machine_config")
    DEVICE_TARGET_FIELD_NUMBER: _ClassVar[int]
    DEVICE_BAUD_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_BOARD_FIELD_NUMBER: _ClassVar[int]
    GRAPH_MODE_FIELD_NUMBER: _ClassVar[int]
    STARTUP_STATE_MACHINE_CONFIG_FIELD_NUMBER: _ClassVar[int]
    device_target: str
    device_baud: int
    expected_board: str
    graph_mode: str
    startup_state_machine_config: str
    def __init__(self, device_target: _Optional[str] = ..., device_baud: _Optional[int] = ..., expected_board: _Optional[str] = ..., graph_mode: _Optional[str] = ..., startup_state_machine_config: _Optional[str] = ...) -> None: ...

class RigConfigurationUpdate(_message.Message):
    __slots__ = ("configuration", "reconnected", "until_restart")
    CONFIGURATION_FIELD_NUMBER: _ClassVar[int]
    RECONNECTED_FIELD_NUMBER: _ClassVar[int]
    UNTIL_RESTART_FIELD_NUMBER: _ClassVar[int]
    configuration: RigConfiguration
    reconnected: bool
    until_restart: bool
    def __init__(self, configuration: _Optional[_Union[RigConfiguration, _Mapping]] = ..., reconnected: _Optional[bool] = ..., until_restart: _Optional[bool] = ...) -> None: ...
