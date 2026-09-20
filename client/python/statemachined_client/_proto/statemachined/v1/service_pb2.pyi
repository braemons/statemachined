from statemachined_client._proto.statemachined.v1 import device_pb2 as _device_pb2
from statemachined_client._proto.statemachined.v1 import documents_pb2 as _documents_pb2
from statemachined_client._proto.statemachined.v1 import recording_pb2 as _recording_pb2
from statemachined_client._proto.statemachined.v1 import rig_configuration_pb2 as _rig_configuration_pb2
from statemachined_client._proto.statemachined.v1 import session_pb2 as _session_pb2
from statemachined_client._proto.statemachined.v1 import state_pb2 as _state_pb2
from statemachined_client._proto.statemachined.v1 import trial_pb2 as _trial_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class ReadStateRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class WatchStateRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ReadObserversRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ReadDeviceRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class OpenLinkRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ReadLinesRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ReadFirmwareRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ReadAutorunRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SaveSettingsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListGraphsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListConfigsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ReadSessionRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class UploadGraphsRequest(_message.Message):
    __slots__ = ("graph_names",)
    GRAPH_NAMES_FIELD_NUMBER: _ClassVar[int]
    graph_names: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, graph_names: _Optional[_Iterable[str]] = ...) -> None: ...

class OpenSessionRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class CloseSessionRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ClearActiveGraphRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ReadRecordingsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class PauseRecordingRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ResumeRecordingRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class StopRecordingRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ClearRecordingRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ReadConfigurationRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ReadHealthRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Health(_message.Message):
    __slots__ = ("ok", "device_connected")
    OK_FIELD_NUMBER: _ClassVar[int]
    DEVICE_CONNECTED_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    device_connected: bool
    def __init__(self, ok: _Optional[bool] = ..., device_connected: _Optional[bool] = ...) -> None: ...

class CancelTrialResult(_message.Message):
    __slots__ = ("trial_id", "cancelled", "outcome_code")
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    CANCELLED_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_CODE_FIELD_NUMBER: _ClassVar[int]
    trial_id: int
    cancelled: bool
    outcome_code: int
    def __init__(self, trial_id: _Optional[int] = ..., cancelled: _Optional[bool] = ..., outcome_code: _Optional[int] = ...) -> None: ...
