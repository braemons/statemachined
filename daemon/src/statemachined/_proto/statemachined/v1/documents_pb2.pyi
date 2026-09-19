from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class StoredFile(_message.Message):
    __slots__ = ("name", "text")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    name: str
    text: str
    def __init__(self, name: _Optional[str] = ..., text: _Optional[str] = ...) -> None: ...

class ReadFileRequest(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class DeleteFileRequest(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class FileDraft(_message.Message):
    __slots__ = ("text",)
    TEXT_FIELD_NUMBER: _ClassVar[int]
    text: str
    def __init__(self, text: _Optional[str] = ...) -> None: ...

class GraphSummary(_message.Message):
    __slots__ = ("name", "readable", "detail", "state_count", "entry")
    NAME_FIELD_NUMBER: _ClassVar[int]
    READABLE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    STATE_COUNT_FIELD_NUMBER: _ClassVar[int]
    ENTRY_FIELD_NUMBER: _ClassVar[int]
    name: str
    readable: bool
    detail: str
    state_count: int
    entry: str
    def __init__(self, name: _Optional[str] = ..., readable: _Optional[bool] = ..., detail: _Optional[str] = ..., state_count: _Optional[int] = ..., entry: _Optional[str] = ...) -> None: ...

class GraphSummaries(_message.Message):
    __slots__ = ("graphs",)
    GRAPHS_FIELD_NUMBER: _ClassVar[int]
    graphs: _containers.RepeatedCompositeFieldContainer[GraphSummary]
    def __init__(self, graphs: _Optional[_Iterable[_Union[GraphSummary, _Mapping]]] = ...) -> None: ...

class GraphValidation(_message.Message):
    __slots__ = ("valid", "detail", "pool_usage", "pool_capacity", "warnings")
    VALID_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    POOL_USAGE_FIELD_NUMBER: _ClassVar[int]
    POOL_CAPACITY_FIELD_NUMBER: _ClassVar[int]
    WARNINGS_FIELD_NUMBER: _ClassVar[int]
    valid: bool
    detail: str
    pool_usage: int
    pool_capacity: int
    warnings: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, valid: _Optional[bool] = ..., detail: _Optional[str] = ..., pool_usage: _Optional[int] = ..., pool_capacity: _Optional[int] = ..., warnings: _Optional[_Iterable[str]] = ...) -> None: ...

class StateMachineConfigSummary(_message.Message):
    __slots__ = ("name", "readable", "detail", "description", "board", "graph_names", "input_line_count", "output_line_count")
    NAME_FIELD_NUMBER: _ClassVar[int]
    READABLE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    BOARD_FIELD_NUMBER: _ClassVar[int]
    GRAPH_NAMES_FIELD_NUMBER: _ClassVar[int]
    INPUT_LINE_COUNT_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_LINE_COUNT_FIELD_NUMBER: _ClassVar[int]
    name: str
    readable: bool
    detail: str
    description: str
    board: str
    graph_names: _containers.RepeatedScalarFieldContainer[str]
    input_line_count: int
    output_line_count: int
    def __init__(self, name: _Optional[str] = ..., readable: _Optional[bool] = ..., detail: _Optional[str] = ..., description: _Optional[str] = ..., board: _Optional[str] = ..., graph_names: _Optional[_Iterable[str]] = ..., input_line_count: _Optional[int] = ..., output_line_count: _Optional[int] = ...) -> None: ...

class StateMachineConfigSummaries(_message.Message):
    __slots__ = ("configs", "loaded")
    CONFIGS_FIELD_NUMBER: _ClassVar[int]
    LOADED_FIELD_NUMBER: _ClassVar[int]
    configs: _containers.RepeatedCompositeFieldContainer[StateMachineConfigSummary]
    loaded: str
    def __init__(self, configs: _Optional[_Iterable[_Union[StateMachineConfigSummary, _Mapping]]] = ..., loaded: _Optional[str] = ...) -> None: ...
