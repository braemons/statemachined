from statemachined._proto.braemons.v1 import trial_outcome_pb2 as _trial_outcome_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TrialCancelReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRIAL_CANCEL_REASON_NONE: _ClassVar[TrialCancelReason]
    TRIAL_CANCEL_REASON_HOST: _ClassVar[TrialCancelReason]
    TRIAL_CANCEL_REASON_LINK_LOST: _ClassVar[TrialCancelReason]
    TRIAL_CANCEL_REASON_ABORT_LINE: _ClassVar[TrialCancelReason]
    TRIAL_CANCEL_REASON_TRIAL_TIMEOUT: _ClassVar[TrialCancelReason]
TRIAL_CANCEL_REASON_NONE: TrialCancelReason
TRIAL_CANCEL_REASON_HOST: TrialCancelReason
TRIAL_CANCEL_REASON_LINK_LOST: TrialCancelReason
TRIAL_CANCEL_REASON_ABORT_LINE: TrialCancelReason
TRIAL_CANCEL_REASON_TRIAL_TIMEOUT: TrialCancelReason

class StateVisit(_message.Message):
    __slots__ = ("state_name", "exit_cause", "fired_transition_position", "fired_transition_target_state_name", "drawn_duration_ms", "entered_device_microseconds", "measured_duration_microseconds")
    STATE_NAME_FIELD_NUMBER: _ClassVar[int]
    EXIT_CAUSE_FIELD_NUMBER: _ClassVar[int]
    FIRED_TRANSITION_POSITION_FIELD_NUMBER: _ClassVar[int]
    FIRED_TRANSITION_TARGET_STATE_NAME_FIELD_NUMBER: _ClassVar[int]
    DRAWN_DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    ENTERED_DEVICE_MICROSECONDS_FIELD_NUMBER: _ClassVar[int]
    MEASURED_DURATION_MICROSECONDS_FIELD_NUMBER: _ClassVar[int]
    state_name: str
    exit_cause: str
    fired_transition_position: int
    fired_transition_target_state_name: str
    drawn_duration_ms: int
    entered_device_microseconds: int
    measured_duration_microseconds: int
    def __init__(self, state_name: _Optional[str] = ..., exit_cause: _Optional[str] = ..., fired_transition_position: _Optional[int] = ..., fired_transition_target_state_name: _Optional[str] = ..., drawn_duration_ms: _Optional[int] = ..., entered_device_microseconds: _Optional[int] = ..., measured_duration_microseconds: _Optional[int] = ...) -> None: ...

class TrialResult(_message.Message):
    __slots__ = ("trial_id", "outcome", "cancel_reason", "total_duration_microseconds", "visits", "path_was_truncated", "first_visit_sequence_number", "total_visit_count")
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    CANCEL_REASON_FIELD_NUMBER: _ClassVar[int]
    TOTAL_DURATION_MICROSECONDS_FIELD_NUMBER: _ClassVar[int]
    VISITS_FIELD_NUMBER: _ClassVar[int]
    PATH_WAS_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    FIRST_VISIT_SEQUENCE_NUMBER_FIELD_NUMBER: _ClassVar[int]
    TOTAL_VISIT_COUNT_FIELD_NUMBER: _ClassVar[int]
    trial_id: int
    outcome: _trial_outcome_pb2.TrialOutcome
    cancel_reason: TrialCancelReason
    total_duration_microseconds: int
    visits: _containers.RepeatedCompositeFieldContainer[StateVisit]
    path_was_truncated: bool
    first_visit_sequence_number: int
    total_visit_count: int
    def __init__(self, trial_id: _Optional[int] = ..., outcome: _Optional[_Union[_trial_outcome_pb2.TrialOutcome, str]] = ..., cancel_reason: _Optional[_Union[TrialCancelReason, str]] = ..., total_duration_microseconds: _Optional[int] = ..., visits: _Optional[_Iterable[_Union[StateVisit, _Mapping]]] = ..., path_was_truncated: _Optional[bool] = ..., first_visit_sequence_number: _Optional[int] = ..., total_visit_count: _Optional[int] = ...) -> None: ...

class DistributionPatch(_message.Message):
    __slots__ = ("name", "minimum_ms", "maximum_ms", "mean_ms", "duration_ms")
    NAME_FIELD_NUMBER: _ClassVar[int]
    MINIMUM_MS_FIELD_NUMBER: _ClassVar[int]
    MAXIMUM_MS_FIELD_NUMBER: _ClassVar[int]
    MEAN_MS_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    name: str
    minimum_ms: int
    maximum_ms: int
    mean_ms: int
    duration_ms: int
    def __init__(self, name: _Optional[str] = ..., minimum_ms: _Optional[int] = ..., maximum_ms: _Optional[int] = ..., mean_ms: _Optional[int] = ..., duration_ms: _Optional[int] = ...) -> None: ...

class ConfigureTrialRequest(_message.Message):
    __slots__ = ("trial_id", "graph", "cap_milliseconds", "start_source", "start_line", "distribution_patches")
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    GRAPH_FIELD_NUMBER: _ClassVar[int]
    CAP_MILLISECONDS_FIELD_NUMBER: _ClassVar[int]
    START_SOURCE_FIELD_NUMBER: _ClassVar[int]
    START_LINE_FIELD_NUMBER: _ClassVar[int]
    DISTRIBUTION_PATCHES_FIELD_NUMBER: _ClassVar[int]
    trial_id: int
    graph: str
    cap_milliseconds: int
    start_source: str
    start_line: int
    distribution_patches: _containers.RepeatedCompositeFieldContainer[DistributionPatch]
    def __init__(self, trial_id: _Optional[int] = ..., graph: _Optional[str] = ..., cap_milliseconds: _Optional[int] = ..., start_source: _Optional[str] = ..., start_line: _Optional[int] = ..., distribution_patches: _Optional[_Iterable[_Union[DistributionPatch, _Mapping]]] = ...) -> None: ...

class ConfigureTrialResult(_message.Message):
    __slots__ = ("trial_id", "graph", "set_version", "graph_index", "elapsed_milliseconds")
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    GRAPH_FIELD_NUMBER: _ClassVar[int]
    SET_VERSION_FIELD_NUMBER: _ClassVar[int]
    GRAPH_INDEX_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_MILLISECONDS_FIELD_NUMBER: _ClassVar[int]
    trial_id: int
    graph: str
    set_version: int
    graph_index: int
    elapsed_milliseconds: int
    def __init__(self, trial_id: _Optional[int] = ..., graph: _Optional[str] = ..., set_version: _Optional[int] = ..., graph_index: _Optional[int] = ..., elapsed_milliseconds: _Optional[int] = ...) -> None: ...

class StartTrialRequest(_message.Message):
    __slots__ = ("trial_id",)
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    trial_id: int
    def __init__(self, trial_id: _Optional[int] = ...) -> None: ...

class StartTrialResult(_message.Message):
    __slots__ = ("trial_id", "started_device_microseconds")
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    STARTED_DEVICE_MICROSECONDS_FIELD_NUMBER: _ClassVar[int]
    trial_id: int
    started_device_microseconds: int
    def __init__(self, trial_id: _Optional[int] = ..., started_device_microseconds: _Optional[int] = ...) -> None: ...

class CancelTrialRequest(_message.Message):
    __slots__ = ("trial_id",)
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    trial_id: int
    def __init__(self, trial_id: _Optional[int] = ...) -> None: ...

class ReadTrialResultRequest(_message.Message):
    __slots__ = ("trial_id",)
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    trial_id: int
    def __init__(self, trial_id: _Optional[int] = ...) -> None: ...
