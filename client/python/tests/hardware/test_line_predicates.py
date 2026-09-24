# SPDX-License-Identifier: LGPL-3.0-or-later
"""Transitions driven from real pins, through the loopback harness.

The host tests run every predicate against an input word a test wrote. Here the
word comes off the board's own pins, raised by the board's own outputs through
eight jumper wires, so a pin table off by one, a port read at the wrong moment,
or an inverted line fails a test here and nowhere else. The native device wires
the same eight in software, which checks everything but the copper.
"""

from __future__ import annotations

from bench_rig import LOOPBACK, input_fed_by, output
from statemachined_client import TrialCancelReason, TrialOutcome

#: Two outputs, and the inputs the harness feeds them to.
OUT_A, OUT_B = 0, 1
IN_A, IN_B = input_fed_by(OUT_A), input_fed_by(OUT_B)


def a_predicate(name: str, raise_lines: list[int], when: dict, **transition) -> dict:
    """One state that raises `raise_lines` and waits for `when`, then HIT."""
    return {
        "name": name,
        "entry": "Wait",
        "states": [
            {
                "name": "Wait",
                "on_entry": [{"line": output(line), "kind": "high"} for line in raise_lines],
                "transitions": [{"when": when, "goto": "Done", **transition}],
            },
            {"name": "Done", "outcome": "HIT"},
        ],
    }


def held_across_a_transition(name: str, *, level: bool) -> dict:
    """Output A raised in state 0 and again in state 1, so input A is already
    high the instant state 1 is entered, and state 1 waits for it.

    Both states raise it because a state lowers what it raised when it exits:
    an entry action in state 1 alone would be read on the *next* scan, which is
    a rising edge one scan after entry -- precisely the case that should fire.
    """
    transition = {"when": {"all": [IN_A]}, "goto": "Done"}
    if level:
        transition["fire_if_already_true_on_entry"] = True
    return {
        "name": name,
        "entry": "Raise",
        "distributions": {"settle": {"kind": "fixed", "duration_ms": 50}},
        "states": [
            {
                "name": "Raise",
                "on_entry": [{"line": output(OUT_A), "kind": "high"}],
                "timeout": {"after": "settle", "goto": "Held"},
            },
            {
                "name": "Held",
                "on_entry": [{"line": output(OUT_A), "kind": "high"}],
                "transitions": [transition],
            },
            {"name": "Done", "outcome": "HIT"},
        ],
    }


def test_the_loopback_harness_is_wired(loopback):
    """Says so once, in a test name, when the wires are missing."""
    assert loopback == LOOPBACK


def test_an_output_driving_an_input_arrives_as_that_line(rig, loopback):
    """The harness itself, asserted before anything is built on it.

    Also the sharpest available check of the line map on real silicon: the one
    place a *pin-to-pin* connection has to agree with the arithmetic at both
    ends at once.
    """
    rig.use(a_predicate("one-line", [OUT_A], {"all": [IN_A]}))
    result = rig.run("one-line", cap_milliseconds=1000)
    assert result.outcome == TrialOutcome.HIT, (
        f"output line {OUT_A} was raised but {IN_A} never went high: check the jumper"
    )
    assert result.visits[0].exit_cause == "transition"


def test_all_requires_every_line_named(rig, loopback):
    """`all` is "both levers held", and the half-held case must not fire.

    The first state raises only output A and must time out rather than fire;
    the second raises B *and re-raises A*, so both inputs are high together.
    Re-raising A is not redundant: exiting a state lowers what it raised, so
    without it the transition would swap A for B rather than add B to it.
    """
    both = {"all": [IN_A, IN_B]}
    rig.use(
        {
            "name": "all-of-two",
            "entry": "HalfHeld",
            "distributions": {"settle": {"kind": "fixed", "duration_ms": 50}},
            "states": [
                {
                    "name": "HalfHeld",
                    "on_entry": [{"line": output(OUT_A), "kind": "high"}],
                    "timeout": {"after": "settle", "goto": "BothHeld"},
                    "transitions": [{"when": both, "goto": "Done"}],
                },
                {
                    "name": "BothHeld",
                    "on_entry": [
                        {"line": output(OUT_A), "kind": "high"},
                        {"line": output(OUT_B), "kind": "high"},
                    ],
                    "transitions": [{"when": both, "goto": "Done"}],
                },
                {"name": "Done", "outcome": "HIT"},
            ],
        }
    )
    result = rig.run("all-of-two", cap_milliseconds=1000)
    assert result.outcome == TrialOutcome.HIT
    assert result.visits[0].exit_cause == "timeout", (
        "an `all` predicate over two lines fired with only one of them high"
    )
    assert result.visits[1].exit_cause == "transition"


def test_any_fires_on_a_single_line_of_several(rig, loopback):
    """`any` is "either lever", so one of the two named lines is enough."""
    rig.use(a_predicate("any-of-two", [OUT_B], {"any": [IN_A, IN_B]}))
    result = rig.run("any-of-two", cap_milliseconds=1000)
    assert result.outcome == TrialOutcome.HIT
    assert result.visits[0].exit_cause == "transition"


def test_none_blocks_a_predicate_that_would_otherwise_match(rig, loopback):
    """`none` is the abort line: "responded, while not holding".

    Input A satisfies `all`, but input B is high too and `none` names it, so the
    predicate must stay false and the trial end on its cap instead.
    """
    rig.use(a_predicate("none-blocks", [OUT_A, OUT_B], {"all": [IN_A], "none": [IN_B]}))
    result = rig.run("none-blocks", cap_milliseconds=300)
    assert result.outcome != TrialOutcome.HIT, (
        "a predicate fired while a line named in `none` was high"
    )
    assert result.visits[0].exit_cause == "cancel"
    assert result.cancel_reason == TrialCancelReason.TRIAL_TIMEOUT, (
        "expected the trial cap to end it"
    )


def test_a_predicate_already_true_on_entry_does_not_fire(rig, loopback):
    """The rising-edge rule, which docs/operations/bringup.md warns looks like a fault.

    A lever the animal is already holding must not end the trial the instant it
    begins: the predicate is true the moment `Held` is entered, and it must
    still wait for a change that never comes, ending on the cap.
    """
    rig.use(held_across_a_transition("edge-only", level=False))
    result = rig.run("edge-only", cap_milliseconds=300)
    assert result.outcome != TrialOutcome.HIT, (
        "a transition fired on a predicate that was already true when its state was entered"
    )


def test_level_makes_a_predicate_fire_on_entry(rig, loopback):
    """`fire_if_already_true_on_entry` is the deliberate opt-out from that rule.

    The same graph, one member different, so what is tested is the flag and not
    the wiring.
    """
    rig.use(held_across_a_transition("level", level=True))
    result = rig.run("level", cap_milliseconds=300)
    assert result.outcome == TrialOutcome.HIT
    assert result.visits[1].exit_cause == "transition"
    # On the state's first evaluation, not after some delay.
    assert result.visits[1].measured_duration_microseconds < 5000, (
        f"a level transition took {result.visits[1].measured_duration_microseconds} us "
        "to fire on entry"
    )
