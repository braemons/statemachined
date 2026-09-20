from statemachined._proto.statemachined.v1 import device_pb2 as _device_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SessionState(_message.Message):
    __slots__ = ("state_machine_config", "committed_set", "session_open", "opened_at_unix_seconds", "open_seconds", "active_graph", "stored_config_names")
    STATE_MACHINE_CONFIG_FIELD_NUMBER: _ClassVar[int]
    COMMITTED_SET_FIELD_NUMBER: _ClassVar[int]
    SESSION_OPEN_FIELD_NUMBER: _ClassVar[int]
    OPENED_AT_UNIX_SECONDS_FIELD_NUMBER: _ClassVar[int]
    OPEN_SECONDS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_GRAPH_FIELD_NUMBER: _ClassVar[int]
    STORED_CONFIG_NAMES_FIELD_NUMBER: _ClassVar[int]
    state_machine_config: LoadedStateMachineConfig
    committed_set: _device_pb2.CommittedGraphSet
    session_open: bool
    opened_at_unix_seconds: float
    open_seconds: float
    active_graph: str
    stored_config_names: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, state_machine_config: _Optional[_Union[LoadedStateMachineConfig, _Mapping]] = ..., committed_set: _Optional[_Union[_device_pb2.CommittedGraphSet, _Mapping]] = ..., session_open: _Optional[bool] = ..., opened_at_unix_seconds: _Optional[float] = ..., open_seconds: _Optional[float] = ..., active_graph: _Optional[str] = ..., stored_config_names: _Optional[_Iterable[str]] = ...) -> None: ...

class LoadedStateMachineConfig(_message.Message):
    __slots__ = ("name", "description", "board", "graph_names", "still_in_the_store")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    BOARD_FIELD_NUMBER: _ClassVar[int]
    GRAPH_NAMES_FIELD_NUMBER: _ClassVar[int]
    STILL_IN_THE_STORE_FIELD_NUMBER: _ClassVar[int]
    name: str
    description: str
    board: str
    graph_names: _containers.RepeatedScalarFieldContainer[str]
    still_in_the_store: bool
    def __init__(self, name: _Optional[str] = ..., description: _Optional[str] = ..., board: _Optional[str] = ..., graph_names: _Optional[_Iterable[str]] = ..., still_in_the_store: _Optional[bool] = ...) -> None: ...

class OpenSessionResult(_message.Message):
    __slots__ = ("state_machine_config", "set_version", "slots", "pool_usage", "pool_capacity", "elapsed_milliseconds")
    class SlotsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    STATE_MACHINE_CONFIG_FIELD_NUMBER: _ClassVar[int]
    SET_VERSION_FIELD_NUMBER: _ClassVar[int]
    SLOTS_FIELD_NUMBER: _ClassVar[int]
    POOL_USAGE_FIELD_NUMBER: _ClassVar[int]
    POOL_CAPACITY_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_MILLISECONDS_FIELD_NUMBER: _ClassVar[int]
    state_machine_config: str
    set_version: int
    slots: _containers.ScalarMap[str, int]
    pool_usage: _device_pb2.GraphPoolCounts
    pool_capacity: _device_pb2.GraphPoolCounts
    elapsed_milliseconds: int
    def __init__(self, state_machine_config: _Optional[str] = ..., set_version: _Optional[int] = ..., slots: _Optional[_Mapping[str, int]] = ..., pool_usage: _Optional[_Union[_device_pb2.GraphPoolCounts, _Mapping]] = ..., pool_capacity: _Optional[_Union[_device_pb2.GraphPoolCounts, _Mapping]] = ..., elapsed_milliseconds: _Optional[int] = ...) -> None: ...

class CloseSessionResult(_message.Message):
    __slots__ = ("was_open", "cancelled_trial_id", "session")
    WAS_OPEN_FIELD_NUMBER: _ClassVar[int]
    CANCELLED_TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_FIELD_NUMBER: _ClassVar[int]
    was_open: bool
    cancelled_trial_id: int
    session: SessionState
    def __init__(self, was_open: _Optional[bool] = ..., cancelled_trial_id: _Optional[int] = ..., session: _Optional[_Union[SessionState, _Mapping]] = ...) -> None: ...

class SetActiveGraphRequest(_message.Message):
    __slots__ = ("graph",)
    GRAPH_FIELD_NUMBER: _ClassVar[int]
    graph: str
    def __init__(self, graph: _Optional[str] = ...) -> None: ...

class ActiveGraph(_message.Message):
    __slots__ = ("active_graph",)
    ACTIVE_GRAPH_FIELD_NUMBER: _ClassVar[int]
    active_graph: str
    def __init__(self, active_graph: _Optional[str] = ...) -> None: ...

class LoadedConfigResult(_message.Message):
    __slots__ = ("loaded", "wiring_pushed", "line_map", "graph_names")
    LOADED_FIELD_NUMBER: _ClassVar[int]
    WIRING_PUSHED_FIELD_NUMBER: _ClassVar[int]
    LINE_MAP_FIELD_NUMBER: _ClassVar[int]
    GRAPH_NAMES_FIELD_NUMBER: _ClassVar[int]
    loaded: str
    wiring_pushed: bool
    line_map: _device_pb2.LineMapView
    graph_names: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, loaded: _Optional[str] = ..., wiring_pushed: _Optional[bool] = ..., line_map: _Optional[_Union[_device_pb2.LineMapView, _Mapping]] = ..., graph_names: _Optional[_Iterable[str]] = ...) -> None: ...
