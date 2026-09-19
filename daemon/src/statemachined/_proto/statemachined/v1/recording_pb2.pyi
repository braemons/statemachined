from statemachined._proto.statemachined.v1 import state_pb2 as _state_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RecordingSegment(_message.Message):
    __slots__ = ("from_entry_number", "to_entry_number", "started_host_time", "ended_host_time", "entry_count")
    FROM_ENTRY_NUMBER_FIELD_NUMBER: _ClassVar[int]
    TO_ENTRY_NUMBER_FIELD_NUMBER: _ClassVar[int]
    STARTED_HOST_TIME_FIELD_NUMBER: _ClassVar[int]
    ENDED_HOST_TIME_FIELD_NUMBER: _ClassVar[int]
    ENTRY_COUNT_FIELD_NUMBER: _ClassVar[int]
    from_entry_number: int
    to_entry_number: int
    started_host_time: str
    ended_host_time: str
    entry_count: int
    def __init__(self, from_entry_number: _Optional[int] = ..., to_entry_number: _Optional[int] = ..., started_host_time: _Optional[str] = ..., ended_host_time: _Optional[str] = ..., entry_count: _Optional[int] = ...) -> None: ...

class RecordingManifest(_message.Message):
    __slots__ = ("name", "description", "state_machine_config", "created_unix_seconds", "created_host_time", "state", "segments", "entry_count", "kind_counts", "unreadable")
    class KindCountsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    STATE_MACHINE_CONFIG_FIELD_NUMBER: _ClassVar[int]
    CREATED_UNIX_SECONDS_FIELD_NUMBER: _ClassVar[int]
    CREATED_HOST_TIME_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    SEGMENTS_FIELD_NUMBER: _ClassVar[int]
    ENTRY_COUNT_FIELD_NUMBER: _ClassVar[int]
    KIND_COUNTS_FIELD_NUMBER: _ClassVar[int]
    UNREADABLE_FIELD_NUMBER: _ClassVar[int]
    name: str
    description: str
    state_machine_config: str
    created_unix_seconds: float
    created_host_time: str
    state: str
    segments: _containers.RepeatedCompositeFieldContainer[RecordingSegment]
    entry_count: int
    kind_counts: _containers.ScalarMap[str, int]
    unreadable: str
    def __init__(self, name: _Optional[str] = ..., description: _Optional[str] = ..., state_machine_config: _Optional[str] = ..., created_unix_seconds: _Optional[float] = ..., created_host_time: _Optional[str] = ..., state: _Optional[str] = ..., segments: _Optional[_Iterable[_Union[RecordingSegment, _Mapping]]] = ..., entry_count: _Optional[int] = ..., kind_counts: _Optional[_Mapping[str, int]] = ..., unreadable: _Optional[str] = ...) -> None: ...

class Recordings(_message.Message):
    __slots__ = ("active", "recordings")
    ACTIVE_FIELD_NUMBER: _ClassVar[int]
    RECORDINGS_FIELD_NUMBER: _ClassVar[int]
    active: RecordingManifest
    recordings: _containers.RepeatedCompositeFieldContainer[RecordingManifest]
    def __init__(self, active: _Optional[_Union[RecordingManifest, _Mapping]] = ..., recordings: _Optional[_Iterable[_Union[RecordingManifest, _Mapping]]] = ...) -> None: ...

class StartRecordingRequest(_message.Message):
    __slots__ = ("name", "description")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    name: str
    description: str
    def __init__(self, name: _Optional[str] = ..., description: _Optional[str] = ...) -> None: ...

class RecordingName(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class ReadRecordingEntriesRequest(_message.Message):
    __slots__ = ("name", "offset", "limit")
    NAME_FIELD_NUMBER: _ClassVar[int]
    OFFSET_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    name: str
    offset: int
    limit: int
    def __init__(self, name: _Optional[str] = ..., offset: _Optional[int] = ..., limit: _Optional[int] = ...) -> None: ...

class RecordingEntries(_message.Message):
    __slots__ = ("name", "offset", "entries", "entry_count", "segments")
    NAME_FIELD_NUMBER: _ClassVar[int]
    OFFSET_FIELD_NUMBER: _ClassVar[int]
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    ENTRY_COUNT_FIELD_NUMBER: _ClassVar[int]
    SEGMENTS_FIELD_NUMBER: _ClassVar[int]
    name: str
    offset: int
    entries: _containers.RepeatedCompositeFieldContainer[_state_pb2.TraceEntry]
    entry_count: int
    segments: _containers.RepeatedCompositeFieldContainer[RecordingSegment]
    def __init__(self, name: _Optional[str] = ..., offset: _Optional[int] = ..., entries: _Optional[_Iterable[_Union[_state_pb2.TraceEntry, _Mapping]]] = ..., entry_count: _Optional[int] = ..., segments: _Optional[_Iterable[_Union[RecordingSegment, _Mapping]]] = ...) -> None: ...
