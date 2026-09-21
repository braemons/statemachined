# SPDX-License-Identifier: AGPL-3.0-or-later
"""One trial, in both directions.

The only conversions in this package that carry a *decision* rather than a
reading: `ConfigureTrialRequest` is a command from another machine, and what it
says is what the board will be armed with.
"""

from __future__ import annotations

from statemachined._proto.braemons.v1 import trial_outcome_pb2
from statemachined._proto.statemachined.v1 import trial_pb2
from statemachined.model.trial_outcome import TrialCancelReason, TrialOutcome
from statemachined.model.trial_record import StateVisitRecord, TrialResultRecord

#: The wire's cancel reasons, by the number the device reports. Written out
#: rather than derived, so a value the proto gains without this daemon
#: learning it is a `Refused` at the seam instead of a silent `NONE` in a
#: record — which is the difference between a finding and a fiction.
_CANCEL_REASON_TO_WIRE = {
    TrialCancelReason.NONE: trial_pb2.TRIAL_CANCEL_REASON_NONE,
    TrialCancelReason.HOST: trial_pb2.TRIAL_CANCEL_REASON_HOST,
    TrialCancelReason.LINK_LOST: trial_pb2.TRIAL_CANCEL_REASON_LINK_LOST,
    TrialCancelReason.ABORT_LINE: trial_pb2.TRIAL_CANCEL_REASON_ABORT_LINE,
    TrialCancelReason.TRIAL_TIMEOUT: trial_pb2.TRIAL_CANCEL_REASON_TRIAL_TIMEOUT,
}


class Refused(Exception):
    """A wire value this daemon cannot honour, with the field that carried it.

    Raised at the seam and turned into a status by `api/servicers/refusals.py`,
    so a servicer never has to know what a bad enum looks like.
    """

    def __init__(self, detail: str, *, context: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.context = context


def state_visit_to_wire(visit: StateVisitRecord) -> trial_pb2.StateVisit:
    """One visit, with `None` kept as absent rather than flattened to zero.

    `fired_transition_position` is the field that matters here: transition zero
    is a real transition, and an exit that was not a transition is not it. A
    sentinel would make those two the same row in an analysis.
    """
    message = trial_pb2.StateVisit(
        state_name=visit.state_name,
        exit_cause=visit.exit_cause,
        drawn_duration_ms=visit.drawn_duration_ms,
        entered_device_microseconds=visit.entered_device_microseconds,
        measured_duration_microseconds=visit.measured_duration_microseconds,
    )
    if visit.fired_transition_position is not None:
        message.fired_transition_position = visit.fired_transition_position
    if visit.fired_transition_target_state_name is not None:
        message.fired_transition_target_state_name = visit.fired_transition_target_state_name
    return message


def trial_result_to_wire(record: TrialResultRecord) -> trial_pb2.TrialResult:
    """A whole trial, as triald will record it.

    The outcome crosses as `braemons.v1.TrialOutcome`, which is neither
    daemon's: this one reports an outcome and triald records one. The number is
    the `.tdr` code and is never renumbered, so passing it through as an int is
    safe — and `tools/check_outcomes.py` is what keeps the two enums the same
    table.
    """
    reason = _CANCEL_REASON_TO_WIRE.get(record.cancel_reason)
    if reason is None:  # pragma: no cover - only a TrialCancelReason with no wire value
        raise Refused(
            f"{record.cancel_reason!r} has no value on the wire", context="cancel_reason"
        )
    return trial_pb2.TrialResult(
        trial_id=record.trial_id,
        # The generated stub types the field as the enum; the value is its
        # int, and the `.tdr` number is the thing that must cross.
        outcome=int(record.outcome),  # ty: ignore[invalid-argument-type]
        cancel_reason=reason,
        total_duration_microseconds=record.total_duration_microseconds,
        visits=[state_visit_to_wire(visit) for visit in record.visits],
        path_was_truncated=record.path_was_truncated,
        first_visit_sequence_number=record.first_visit_sequence_number,
        total_visit_count=record.total_visit_count,
    )


def distribution_patches_from_wire(
    patches: list[trial_pb2.DistributionPatch],
) -> list[dict]:
    """Per-trial distribution overrides, as the device layer takes them.

    **Only the parameters that were set.** Every field is `optional` on the
    wire precisely so that "leave this one alone" is expressible, and zero is a
    legal duration — so presence is the question, not truthiness.
    """
    fields = ("minimum_ms", "maximum_ms", "mean_ms", "duration_ms")
    decoded = []
    for patch in patches:
        if not patch.name:
            raise Refused("a distribution patch names no distribution", context="name")
        values = {name: getattr(patch, name) for name in fields if patch.HasField(name)}
        if not values:
            raise Refused(
                f"the patch for {patch.name!r} sets no parameter",
                context="distribution_patches",
            )
        decoded.append({"name": patch.name} | values)
    return decoded


def configure_trial_from_wire(request: trial_pb2.ConfigureTrialRequest) -> dict:
    """What arming one trial needs, as `RigService.configure_trial` takes it.

    `graph` is left empty rather than resolved here: which graph an empty name
    means is the session's business (the active graph), and the seam's job is
    to say what arrived rather than to decide.
    """
    if request.trial_id < 0:
        raise Refused("a trial id is never negative", context="trial_id")
    if request.cap_milliseconds < 0:
        raise Refused("a cap is never negative", context="cap_milliseconds")
    return {
        "trial_id": request.trial_id,
        "graph_name": request.graph,
        "cap_milliseconds": request.cap_milliseconds,
        "start_source": request.start_source or "serial",
        "start_line": request.start_line if request.HasField("start_line") else None,
        "distribution_patches": distribution_patches_from_wire(
            list(request.distribution_patches)
        ),
    }


def outcome_name(code: int) -> str:
    """The `.tdr` name for a code, or the code as a string.

    A code this build has never seen is a *newer* board or a newer triald, not
    a broken one — the taxonomy grows by addition — so it is reported as
    itself rather than refused or read as `NOT_STARTED`.
    """
    try:
        return TrialOutcome(code).name
    except ValueError:
        return str(code)


_ = trial_outcome_pb2  # the enum's home; imported so the descriptor is registered
