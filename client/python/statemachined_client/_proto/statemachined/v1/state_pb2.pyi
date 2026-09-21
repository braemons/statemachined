from google.protobuf import struct_pb2 as _struct_pb2
from statemachined_client._proto.statemachined.v1 import device_pb2 as _device_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RigState(_message.Message):
    __slots__ = ("connected", "link_state", "running", "trial_id", "graph", "state_name", "state_index", "input_word", "output_word", "scan", "newest_trace_entry_number")
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    LINK_STATE_FIELD_NUMBER: _ClassVar[int]
    RUNNING_FIELD_NUMBER: _ClassVar[int]
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    GRAPH_FIELD_NUMBER: _ClassVar[int]
    STATE_NAME_FIELD_NUMBER: _ClassVar[int]
    STATE_INDEX_FIELD_NUMBER: _ClassVar[int]
    INPUT_WORD_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_WORD_FIELD_NUMBER: _ClassVar[int]
    SCAN_FIELD_NUMBER: _ClassVar[int]
    NEWEST_TRACE_ENTRY_NUMBER_FIELD_NUMBER: _ClassVar[int]
    connected: bool
    link_state: int
    running: bool
    trial_id: int
    graph: str
    state_name: str
    state_index: int
    input_word: int
    output_word: int
    scan: _device_pb2.ScanHealth
    newest_trace_entry_number: int
    def __init__(self, connected: _Optional[bool] = ..., link_state: _Optional[int] = ..., running: _Optional[bool] = ..., trial_id: _Optional[int] = ..., graph: _Optional[str] = ..., state_name: _Optional[str] = ..., state_index: _Optional[int] = ..., input_word: _Optional[int] = ..., output_word: _Optional[int] = ..., scan: _Optional[_Union[_device_pb2.ScanHealth, _Mapping]] = ..., newest_trace_entry_number: _Optional[int] = ...) -> None: ...

class TraceEntry(_message.Message):
    __slots__ = ("entry_number", "kind", "recorded_host_time", "trial_id", "payload")
    ENTRY_NUMBER_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    RECORDED_HOST_TIME_FIELD_NUMBER: _ClassVar[int]
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    entry_number: int
    kind: str
    recorded_host_time: str
    trial_id: int
    payload: _struct_pb2.Struct
    def __init__(self, entry_number: _Optional[int] = ..., kind: _Optional[str] = ..., recorded_host_time: _Optional[str] = ..., trial_id: _Optional[int] = ..., payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class TraceWindow(_message.Message):
    __slots__ = ("entries", "newest_entry_number", "oldest_entry_number_still_held", "ring_capacity", "lost_entries_before")
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    NEWEST_ENTRY_NUMBER_FIELD_NUMBER: _ClassVar[int]
    OLDEST_ENTRY_NUMBER_STILL_HELD_FIELD_NUMBER: _ClassVar[int]
    RING_CAPACITY_FIELD_NUMBER: _ClassVar[int]
    LOST_ENTRIES_BEFORE_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[TraceEntry]
    newest_entry_number: int
    oldest_entry_number_still_held: int
    ring_capacity: int
    lost_entries_before: int
    def __init__(self, entries: _Optional[_Iterable[_Union[TraceEntry, _Mapping]]] = ..., newest_entry_number: _Optional[int] = ..., oldest_entry_number_still_held: _Optional[int] = ..., ring_capacity: _Optional[int] = ..., lost_entries_before: _Optional[int] = ...) -> None: ...

class ReadTraceRequest(_message.Message):
    __slots__ = ("since_entry_number", "limit")
    SINCE_ENTRY_NUMBER_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    since_entry_number: int
    limit: int
    def __init__(self, since_entry_number: _Optional[int] = ..., limit: _Optional[int] = ...) -> None: ...

class WatchTraceRequest(_message.Message):
    __slots__ = ("since_entry_number",)
    SINCE_ENTRY_NUMBER_FIELD_NUMBER: _ClassVar[int]
    since_entry_number: int
    def __init__(self, since_entry_number: _Optional[int] = ...) -> None: ...

class ReadTrialTraceRequest(_message.Message):
    __slots__ = ("trial_id",)
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    trial_id: int
    def __init__(self, trial_id: _Optional[int] = ...) -> None: ...

class TrialTrace(_message.Message):
    __slots__ = ("trial_id", "entries")
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    trial_id: int
    entries: _containers.RepeatedCompositeFieldContainer[TraceEntry]
    def __init__(self, trial_id: _Optional[int] = ..., entries: _Optional[_Iterable[_Union[TraceEntry, _Mapping]]] = ...) -> None: ...

class Observer(_message.Message):
    __slots__ = ("observer_id", "name", "stream", "address", "connected_at_unix_seconds", "connected_seconds", "delivered", "fell_behind")
    OBSERVER_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STREAM_FIELD_NUMBER: _ClassVar[int]
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_AT_UNIX_SECONDS_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_SECONDS_FIELD_NUMBER: _ClassVar[int]
    DELIVERED_FIELD_NUMBER: _ClassVar[int]
    FELL_BEHIND_FIELD_NUMBER: _ClassVar[int]
    observer_id: str
    name: str
    stream: str
    address: str
    connected_at_unix_seconds: float
    connected_seconds: float
    delivered: int
    fell_behind: bool
    def __init__(self, observer_id: _Optional[str] = ..., name: _Optional[str] = ..., stream: _Optional[str] = ..., address: _Optional[str] = ..., connected_at_unix_seconds: _Optional[float] = ..., connected_seconds: _Optional[float] = ..., delivered: _Optional[int] = ..., fell_behind: _Optional[bool] = ...) -> None: ...

class Observers(_message.Message):
    __slots__ = ("observers", "count")
    OBSERVERS_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    observers: _containers.RepeatedCompositeFieldContainer[Observer]
    count: int
    def __init__(self, observers: _Optional[_Iterable[_Union[Observer, _Mapping]]] = ..., count: _Optional[int] = ...) -> None: ...

class StateFrame(_message.Message):
    __slots__ = ("sequence", "state")
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    sequence: int
    state: RigState
    def __init__(self, sequence: _Optional[int] = ..., state: _Optional[_Union[RigState, _Mapping]] = ...) -> None: ...
