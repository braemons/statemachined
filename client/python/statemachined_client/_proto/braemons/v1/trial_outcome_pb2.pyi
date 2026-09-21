from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from typing import ClassVar as _ClassVar

DESCRIPTOR: _descriptor.FileDescriptor

class TrialOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NOT_STARTED: _ClassVar[TrialOutcome]
    UNDETERMINED: _ClassVar[TrialOutcome]
    HIT: _ClassVar[TrialOutcome]
    WRONG_RESPONSE: _ClassVar[TrialOutcome]
    EARLY_HIT: _ClassVar[TrialOutcome]
    EARLY_WRONG_RESPONSE: _ClassVar[TrialOutcome]
    EARLY: _ClassVar[TrialOutcome]
    LATE: _ClassVar[TrialOutcome]
    EYE_ERROR: _ClassVar[TrialOutcome]
    UNEXPECTED_START_SIGNAL: _ClassVar[TrialOutcome]
    WRONG_START_SIGNAL: _ClassVar[TrialOutcome]
    CANCELLED: _ClassVar[TrialOutcome]
    NEVER_FINISHED: _ClassVar[TrialOutcome]
NOT_STARTED: TrialOutcome
UNDETERMINED: TrialOutcome
HIT: TrialOutcome
WRONG_RESPONSE: TrialOutcome
EARLY_HIT: TrialOutcome
EARLY_WRONG_RESPONSE: TrialOutcome
EARLY: TrialOutcome
LATE: TrialOutcome
EYE_ERROR: TrialOutcome
UNEXPECTED_START_SIGNAL: TrialOutcome
WRONG_START_SIGNAL: TrialOutcome
CANCELLED: TrialOutcome
NEVER_FINISHED: TrialOutcome
