# SPDX-License-Identifier: GPL-3.0-or-later
"""What the authored form refuses, and why each refusal is worth the code.

A graph is user data. It arrives from a file somebody edited or from an HTTP
request, it will be run three hundred times without anybody watching, and the
failures that matter are the quiet ones: a state nothing can reach, a reward
pulse with no width, a terminal state that also leads somewhere. Every test here
is one of those.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from statemachined.model.graph import (
    ChoiceDuration,
    GraphDefinition,
    OutputActionSpecification,
    StateDefinition,
    TransitionPredicate,
    UniformDuration,
)


def minimal_graph_dictionary(**overrides) -> dict:
    """wait --(500 ms)--> Hit. The smallest thing that is a graph at all."""
    graph = {
        "name": "minimal",
        "entry": "Wait",
        "distributions": {"dwell": {"kind": "fixed", "duration_ms": 500}},
        "states": [
            {"name": "Wait", "timeout": {"after": "dwell", "goto": "Hit"}},
            {"name": "Hit", "outcome": "HIT"},
        ],
    }
    graph.update(overrides)
    return graph


def test_a_minimal_graph_is_accepted():
    graph = GraphDefinition.model_validate(minimal_graph_dictionary())
    assert graph.state_names_in_declaration_order == ["Wait", "Hit"]
    assert graph.state_named("Hit").is_terminal


def test_a_graph_with_no_reachable_terminal_state_is_refused():
    # It would run until the trial cap fired, with whatever it raised still
    # high, on every trial. The cap is the backstop; this is the diagnosis.
    with pytest.raises(ValidationError, match="cannot end"):
        GraphDefinition.model_validate(
            minimal_graph_dictionary(
                states=[
                    {"name": "Wait", "timeout": {"after": "dwell", "goto": "Wait"}},
                ]
            )
        )


def test_an_unreachable_state_is_refused_rather_than_pruned():
    # Almost always a typo in the name of the state that should have led to it.
    # Dropping it silently hides that until somebody wonders why a condition
    # never occurs.
    with pytest.raises(ValidationError, match="cannot reach these states.*Orphan"):
        GraphDefinition.model_validate(
            minimal_graph_dictionary(
                states=[
                    {"name": "Wait", "timeout": {"after": "dwell", "goto": "Hit"}},
                    {"name": "Hit", "outcome": "HIT"},
                    {"name": "Orphan", "outcome": "LATE"},
                ]
            )
        )


def test_a_transition_to_a_state_that_does_not_exist_names_what_does():
    with pytest.raises(ValidationError, match="Hitt.*Has: Wait, Hit"):
        GraphDefinition.model_validate(
            minimal_graph_dictionary(
                states=[
                    {
                        "name": "Wait",
                        "timeout": {"after": "dwell", "goto": "Hit"},
                        "transitions": [{"when": {"all": ["lever"]}, "goto": "Hitt"}],
                    },
                    {"name": "Hit", "outcome": "HIT"},
                ]
            )
        )


def test_a_timeout_drawing_from_a_distribution_that_does_not_exist_is_refused():
    with pytest.raises(ValidationError, match="foreperiod.*not a distribution"):
        GraphDefinition.model_validate(
            minimal_graph_dictionary(
                states=[
                    {"name": "Wait", "timeout": {"after": "foreperiod", "goto": "Hit"}},
                    {"name": "Hit", "outcome": "HIT"},
                ]
            )
        )


def test_an_entry_state_that_is_not_a_state_is_refused():
    with pytest.raises(ValidationError, match="entry state 'Start' is not a state"):
        GraphDefinition.model_validate(minimal_graph_dictionary(entry="Start"))


def test_two_states_with_the_same_name_are_refused():
    with pytest.raises(ValidationError, match="two states called 'Wait'"):
        GraphDefinition.model_validate(
            minimal_graph_dictionary(
                states=[
                    {"name": "Wait", "timeout": {"after": "dwell", "goto": "Hit"}},
                    {"name": "Wait", "outcome": "LATE"},
                    {"name": "Hit", "outcome": "HIT"},
                ]
            )
        )


def test_an_unknown_field_is_refused_rather_than_ignored():
    # A misspelled key that is silently dropped is a paradigm quietly missing
    # the thing its author wrote down.
    with pytest.raises(ValidationError):
        GraphDefinition.model_validate(minimal_graph_dictionary(entrypoint="Wait"))


# --------------------------------------------------------- terminal states ---


def test_a_terminal_state_that_also_leads_somewhere_is_refused():
    with pytest.raises(ValidationError, match="terminal and also leads somewhere"):
        StateDefinition.model_validate(
            {"name": "Hit", "outcome": "HIT", "timeout": {"after": "dwell", "goto": "Wait"}}
        )


def test_a_terminal_state_may_still_raise_lines_on_entry():
    # It is how a reward is written: "pulse the valve on entering Hit". Nothing
    # can lower it by exiting, which is deliberate -- the lamp stays lit as a
    # result lamp until the next trial starts.
    state = StateDefinition.model_validate(
        {
            "name": "Hit",
            "outcome": "HIT",
            "on_entry": [{"line": "valve", "kind": "pulse", "pulse_ms": 40}],
        }
    )
    assert state.is_terminal


def test_a_terminal_state_with_exit_actions_is_refused():
    with pytest.raises(ValidationError, match="on_exit actions, which can never run"):
        StateDefinition.model_validate(
            {"name": "Hit", "outcome": "HIT", "on_exit": [{"line": "lamp", "kind": "low"}]}
        )


def test_an_outcome_that_is_not_one_lists_the_ones_that_are():
    with pytest.raises(ValidationError, match="Legal outcomes:.*WRONG_RESPONSE"):
        StateDefinition.model_validate({"name": "Hit", "outcome": "SUCCESS"})


def test_undetermined_is_not_an_outcome_a_graph_may_declare():
    # It is the value a trial has while it is still running. A state declaring
    # it would report "still running" for ever.
    with pytest.raises(ValidationError):
        StateDefinition.model_validate({"name": "Hit", "outcome": "UNDETERMINED"})


# ---------------------------------------------------------------- actions ---


def test_a_pulse_with_no_width_is_refused():
    # It would raise a line and schedule its fall for the same instant, so
    # whether it reaches a pin depends on when the scan lands. A reward that is
    # silently nothing is the failure this prevents.
    with pytest.raises(ValidationError, match="needs a positive pulse_ms"):
        OutputActionSpecification.model_validate({"line": "valve", "kind": "pulse"})


def test_a_width_on_something_that_is_not_a_pulse_is_refused():
    with pytest.raises(ValidationError, match="pulse_ms means nothing"):
        OutputActionSpecification.model_validate(
            {"line": "lamp", "kind": "high", "pulse_ms": 40}
        )


# ------------------------------------------------------------- predicates ---


def test_a_predicate_that_names_no_lines_is_refused():
    # It would fire on the first evaluation of every state it is in.
    with pytest.raises(ValidationError, match="names no lines"):
        TransitionPredicate.model_validate({})


# ------------------------------------------------------------ durations ---


def test_an_inverted_uniform_interval_is_refused():
    with pytest.raises(ValidationError, match="below minimum_ms"):
        UniformDuration.model_validate({"kind": "uniform", "minimum_ms": 700, "maximum_ms": 300})


def test_choice_weights_must_line_up_with_the_options():
    # A short weights array would silently give the unlisted options weight 1
    # against neighbours weighted in the hundreds.
    with pytest.raises(ValidationError, match="they name the same choices"):
        ChoiceDuration.model_validate(
            {"kind": "choice", "options_ms": [100, 200, 300], "weights": [1, 1]}
        )


def test_choice_weights_that_are_all_zero_are_refused():
    with pytest.raises(ValidationError, match="nothing could ever be drawn"):
        ChoiceDuration.model_validate({"kind": "choice", "options_ms": [100, 200], "weights": [0, 0]})
