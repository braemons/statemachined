# SPDX-License-Identifier: LGPL-3.0-or-later
"""What came back from a trial, with the indices turned back into names.

The device reports a path of six-element arrays (docs/reference/protocol.md 4.3) and the
`visit` stream reports the same rows as they happen (4.4). Both are indices,
because the device has 32 KB. Both are decoded here, by the same code, into the
names the graph was authored with -- so an analysis reads `Foreperiod` and not
`state 2`, and the trace in 4.6 is joinable to the graph a person edited.

Nothing here interprets. The daemon reports what the device measured and sets no
veto field; a record is evidence, and "not a second decision authority" applies
here more than anywhere, because a log is exactly where an opinion gets smuggled
in unnoticed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from .trial_outcome import TrialCancelReason, TrialOutcome

#: `transition_index` when the state was not left by a transition. The wire's
#: `kNoTransition`, and the reason position 2 of a row is not simply an int.
NO_TRANSITION_FIRED = 255

#: The number of elements in a `result_path` row and in a `visit`'s `v`. One
#: shape for one fact: two would be how the record and the stream drift apart.
WIRE_ROW_LENGTH = 6


class StateVisitRecord(BaseModel):
    """One state, entered and left, with everything the device measured about it."""

    model_config = ConfigDict(extra="forbid")

    state_name: str
    exit_cause: str  # "timeout" | "transition" | "cancel" | "terminal"

    #: Which of this state's transitions fired, counted from zero in the order
    #: the graph declares them, or None for an exit that was not a transition.
    fired_transition_position: int | None = None
    #: Where that transition led, resolved against the graph. None when nothing
    #: fired, and also when the graph the daemon holds no longer has that edge.
    fired_transition_target_state_name: str | None = None

    #: What the device *drew* for this state's timeout, in milliseconds.
    #: Reported so a random timing is evidence in the record and not merely
    #: reproducible from the seed.
    drawn_duration_ms: int
    #: Device clock, microseconds. Wraps every ~71 minutes, which is why the
    #: daemon correlates it to the host clock rather than handing it over raw.
    entered_device_microseconds: int
    #: What actually happened. `drawn_duration_ms` is what was asked for.
    measured_duration_microseconds: int


class TrialResultRecord(BaseModel):
    """A whole trial's result, reassembled and named."""

    model_config = ConfigDict(extra="forbid")

    trial_id: int
    #: An IntEnum, because the wire carries the number and it is a contract --
    #: these values are in every .tdr the lab has written. It is serialised by
    #: **name** wherever this model becomes JSON: a `.tdr` needs the number and
    #: a person reading an API response needs the word, and the two audiences
    #: are not the same one.
    outcome: TrialOutcome
    cancel_reason: TrialCancelReason = TrialCancelReason.NONE
    total_duration_microseconds: int = 0

    visits: list[StateVisitRecord] = Field(default_factory=list)

    #: The device's path buffer is a ring and drops its oldest entries, so a
    #: long looping trial arrives with a window rather than the whole run.
    #: `visits` then holds the last `len(visits)` of `total_visit_count`,
    #: starting at `first_visit_sequence_number`.
    path_was_truncated: bool = False
    first_visit_sequence_number: int = 0
    total_visit_count: int = 0

    @field_serializer("outcome")
    def _serialise_outcome_by_name(self, outcome: TrialOutcome) -> str:
        return outcome.name

    @field_serializer("cancel_reason")
    def _serialise_cancel_reason_by_name(self, reason: TrialCancelReason) -> str:
        return reason.name

    @property
    def missing_visit_count(self) -> int:
        """How many visits the ring dropped. Zero unless it wrapped."""
        return max(0, self.total_visit_count - len(self.visits))


def decode_state_visit_row(
    row: list[int | str],
    state_names_by_index: list[str],
    transition_target_names_by_state_index: list[list[str]],
) -> StateVisitRecord:
    """One wire row into a named visit.

    `state_names_by_index` and `transition_target_names_by_state_index` come from
    the compiled graph -- see `statemachined.graph_set_compiler` -- so decoding is only
    possible against the graph the trial actually ran, which is the property
    that keeps a renamed state from silently mislabelling last week's data.

    An index the graph does not explain is reported rather than guessed at: a
    row that arrived from a device holding a different set is a finding, and a
    plausible-looking name for it would bury that.
    """
    if len(row) != WIRE_ROW_LENGTH:
        raise ValueError(f"a path row has {len(row)} elements, not {WIRE_ROW_LENGTH}: {row!r}")

    state_index, exit_cause, transition_index, drawn_ms, entered_us, duration_us = row
    if not isinstance(state_index, int) or not (0 <= state_index < len(state_names_by_index)):
        raise ValueError(
            f"a path row names state {state_index!r}, and the graph has "
            f"{len(state_names_by_index)} states"
        )

    fired_position: int | None = None
    fired_target: str | None = None
    if transition_index != NO_TRANSITION_FIRED:
        fired_position = int(transition_index)
        targets = transition_target_names_by_state_index[state_index]
        if 0 <= fired_position < len(targets):
            fired_target = targets[fired_position]

    return StateVisitRecord(
        state_name=state_names_by_index[state_index],
        exit_cause=str(exit_cause),
        fired_transition_position=fired_position,
        fired_transition_target_state_name=fired_target,
        drawn_duration_ms=int(drawn_ms),
        entered_device_microseconds=int(entered_us),
        measured_duration_microseconds=int(duration_us),
    )
