# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading a result back into the names the graph was written with."""

from __future__ import annotations

import pytest

from statemachined.model.trial_outcome import TrialOutcome, terminal_outcome_for_name
from statemachined.model.trial_record import (
    NO_TRANSITION_FIRED,
    TrialResultRecord,
    decode_state_visit_row,
)

STATE_NAMES = ["Wait", "Cue", "Hit", "Aborted"]
TRANSITION_TARGETS = [["Aborted"], ["Hit", "Aborted"], [], []]


def test_a_row_becomes_the_names_the_graph_was_written_with():
    visit = decode_state_visit_row(
        [1, "transition", 0, 500, 500120, 183044], STATE_NAMES, TRANSITION_TARGETS
    )
    assert visit.state_name == "Cue"
    assert visit.exit_cause == "transition"
    assert visit.fired_transition_position == 0
    assert visit.fired_transition_target_state_name == "Hit"
    assert visit.drawn_duration_ms == 500
    assert visit.measured_duration_microseconds == 183044


def test_the_transition_index_is_read_against_the_state_that_fired_it():
    # It is "the n'th transition of Cue", not an index into the shared pool, so
    # the same 1 means a different edge in a different state.
    visit = decode_state_visit_row(
        [1, "transition", 1, 0, 0, 10], STATE_NAMES, TRANSITION_TARGETS
    )
    assert visit.fired_transition_target_state_name == "Aborted"


def test_an_exit_that_was_not_a_transition_names_none():
    visit = decode_state_visit_row(
        [0, "timeout", NO_TRANSITION_FIRED, 500, 0, 500120], STATE_NAMES, TRANSITION_TARGETS
    )
    assert visit.fired_transition_position is None
    assert visit.fired_transition_target_state_name is None


def test_a_state_index_the_graph_cannot_explain_is_a_finding_not_a_guess():
    # A row from a device holding a different set. A plausible-looking name for
    # it would bury exactly the thing worth knowing.
    with pytest.raises(ValueError, match="names state 9, and the graph has 4 states"):
        decode_state_visit_row([9, "timeout", 255, 0, 0, 0], STATE_NAMES, TRANSITION_TARGETS)


def test_a_row_of_the_wrong_length_is_refused():
    with pytest.raises(ValueError, match="has 5 elements, not 6"):
        decode_state_visit_row([0, "timeout", 255, 0, 0], STATE_NAMES, TRANSITION_TARGETS)


def test_a_truncated_path_says_how_much_is_missing():
    # The device's ring drops its oldest entries, so a long looping trial
    # arrives as a window. How much is gone is arithmetic the record should do
    # once rather than every reader doing it again.
    result = TrialResultRecord(
        trial_id=193,
        outcome=TrialOutcome.HIT,
        path_was_truncated=True,
        first_visit_sequence_number=45,
        total_visit_count=300,
        visits=[],
    )
    assert result.missing_visit_count == 300


def test_an_outcome_name_that_is_not_one_lists_the_ones_that_are():
    assert terminal_outcome_for_name("EARLY_WRONG_RESPONSE") is TrialOutcome.EARLY_WRONG_RESPONSE
    with pytest.raises(ValueError, match="Legal outcomes:.*NOT_STARTED"):
        terminal_outcome_for_name("MISS")
