# SPDX-License-Identifier: GPL-3.0-or-later
"""Predicates over real pins, using the board to drive its own inputs.

This file closes the one gap nothing else in the repository covers. The three
masks in `firmware/core/graph/transition.h` are exercised thoroughly on the
host, and Renode checks that an input pin arrives as the line number a graph
would name -- but no test anywhere drives a *predicate* from real silicon. Both
the Renode trial and the one in test_trial.py end by timeout, on purpose; the
chain pin -> InputConditioner -> Transition::matches() -> a transition firing
has never run on a board.

It can, without anybody pressing anything, because the board has outputs as
well as inputs: jumper an output line to an input line and a graph's entry
action drives its own predicate one scan later. That also makes testable the
thing a person physically cannot do reliably -- release two switches and press
them again *within the same millisecond*, which is what a rising-edge rule over
a two-line predicate needs.

    Output line 0  D10 ──────► D6   Input line 4
    Output line 1  D11 ──────► D7   Input line 5
    Output line 2  D12 ──────► D8   Input line 6

Three jumper wires, no components. dev/HARDWARE.md has the full line map. The
tests skip themselves with these instructions when the wires are not there, so
the suite stays something you can run against a bare board.

**Inputs 4-6, not 0-2.** BRINGUP.md §2 wires the demo's switches as a contact to
5 V, so a jumper driving input 0 or 1 would be fighting a closed switch -- an
output pulling low against 5 V. Inputs 4-6 are untouched by §2, so a board can
carry the demo wiring and this harness at once. Sharing the output pins is
harmless the other way round: a pin drives an LED and a jumper equally well.

One consequence of using outputs as the stimulus is that a line driving an input
can no longer be asserted on independently, which is why test_trial.py raises
output line 3 to check that an entry action reaches a pin, and not one of these.
"""

from __future__ import annotations

import pytest
from harness import GraphUpload, Outcome, read_result
from statemachined.device.messages import Field, MsgType

#: output line -> input line, as the jumpers above wire them.
LOOPBACK = {0: 4, 1: 5, 2: 6}

#: Named, because a test that says `bit(0)` in an action and `bit(0)` in a
#: predicate is a test that will one day be read as though they were the same
#: line. They are two different pins with a wire between them.
OUT_A, OUT_B = 0, 1
IN_A, IN_B = LOOPBACK[OUT_A], LOOPBACK[OUT_B]

pytestmark = pytest.mark.loopback


def bit(n: int) -> int:
    return 1 << n


def run_trial(device, graph, trial_id: int, cap_ms: int = 2000):
    """Arm, start, and collect the result. The shape every test here shares."""
    armed = device.request(
        MsgType.CONFIGURE, trial_id=trial_id, set_version=graph.version, cap_ms=cap_ms,
        start="serial",
    )
    assert armed[Field.MSG_TYPE] == MsgType.ARMED
    started = device.request(MsgType.START, trial_id=trial_id)
    assert started[Field.MSG_TYPE] == MsgType.STARTED
    return read_result(device.session)


def predicate_graph(device, version: int, raise_lines: list[int], **predicate):
    """A graph that drives some outputs, then waits for a predicate on the inputs.

    State 0 raises `raise_lines` on entry and holds until the predicate matches;
    state 1 is terminal with outcome HIT. If the predicate never matches, the
    trial cap ends it instead and the outcome is not HIT -- which is how a test
    here distinguishes "the predicate fired" from "the predicate should not have
    fired" without waiting on a timeout it cannot tell apart from a hang.
    """
    graph = GraphUpload(device.session, version=version)
    graph.begin(n_states=2, entry=0)
    graph.state(0, terminal=None, timeout=None)
    for line in raise_lines:
        graph.action("entry", line=line, kind="high")
    graph.transition(target=1, **predicate)
    graph.state(1, terminal=int(Outcome.HIT), timeout=None)
    ok = graph.end()
    assert ok[Field.MSG_TYPE] == MsgType.SET_OK, ok
    return graph


def test_the_loopback_harness_is_wired(loopback):
    """Names the wires when they are missing, instead of failing four times.

    The `loopback` fixture is what does the probing; this test exists so that a
    run against a bare board says so once, in a test name, rather than only in
    a skip reason nobody reads.
    """
    assert loopback == LOOPBACK


def test_an_output_driving_an_input_arrives_as_that_line(device, loopback):
    """The harness itself, asserted before anything is built on it.

    Also the sharpest available check of the line map on real silicon: it is the
    only test in the repository where a *pin-to-pin* connection has to agree
    with the arithmetic at both ends at once.
    """
    graph = predicate_graph(device, version=11, raise_lines=[OUT_A], all=bit(IN_A))
    result = run_trial(device, graph, trial_id=101)
    assert result.outcome == Outcome.HIT, (
        f"output line {OUT_A} was raised but input line {IN_A} never went high: "
        "check the D10 -> D6 jumper"
    )
    assert result.visit(0).cause == "transition"


def test_all_requires_every_line_named(device, loopback):
    """`all` is "both levers held", and the half-held case must not fire.

    Two states of stimulus in one trial: the first raises only output A and the
    predicate must not match, then output B joins it and it must. A graph that
    fired on the first would be the classic bug of testing `w & all_high != 0`
    rather than `== all_high`, and only the two-line case can see it.
    """
    graph = GraphUpload(device.session, version=12)
    graph.begin(n_states=3, entry=0)
    # State 0 raises output A alone and moves on after 50 ms, whatever the
    # predicate thinks. If `all` were wrong, the trial would end here instead.
    graph.dist(0, kind="fixed", a=50)
    graph.state(0, terminal=None, timeout={"dist": 0, "target": 1})
    graph.action("entry", line=OUT_A, kind="high")
    graph.transition(target=2, all=bit(IN_A) | bit(IN_B))
    # State 1 raises B *and re-raises A*, so that both inputs are high together.
    #
    # Re-raising A is not redundant. Exiting a state lowers every line that state
    # raised, so without this the transition would swap A for B rather than add
    # B to it, and `all` would never see the two lines high at once -- which is
    # how this test failed the first time it ran against real wires.
    # state_machine.cpp:158 lowers on exit only `& ~ops.set_high`, so a line the
    # entering state also raises stays up across the transition without a glitch.
    graph.state(1, terminal=None, timeout=None)
    graph.action("entry", line=OUT_A, kind="high")
    graph.action("entry", line=OUT_B, kind="high")
    graph.transition(target=2, all=bit(IN_A) | bit(IN_B))
    graph.state(2, terminal=int(Outcome.HIT), timeout=None)
    ok = graph.end()
    assert ok[Field.MSG_TYPE] == MsgType.SET_OK, ok

    result = run_trial(device, graph, trial_id=102)
    assert result.outcome == Outcome.HIT
    # Three visits: the half-held state timed out rather than transitioning,
    # which is the whole assertion.
    assert result.visit(0).cause == "timeout", (
        "an `all` predicate over two lines fired with only one of them high"
    )
    assert result.visit(1).cause == "transition"


def test_any_fires_on_a_single_line_of_several(device, loopback):
    """`any` is "either lever", so one of the two named lines is enough."""
    graph = predicate_graph(device, version=13, raise_lines=[OUT_B], any=bit(IN_A) | bit(IN_B))
    result = run_trial(device, graph, trial_id=103)
    assert result.outcome == Outcome.HIT
    assert result.visit(0).cause == "transition"


def test_none_blocks_a_predicate_that_would_otherwise_match(device, loopback):
    """`none` is the abort line: "responded, while not holding".

    Input A is raised and satisfies `all`, but input B is raised too and `none`
    names it, so the predicate must stay false and the trial must end on its
    cap instead. This is the combination test_trial_runner.cpp:145 runs on the
    host -- here it runs through real pins.
    """
    graph = predicate_graph(
        device, version=14, raise_lines=[OUT_A, OUT_B], all=bit(IN_A), none=bit(IN_B)
    )
    result = run_trial(device, graph, trial_id=104, cap_ms=300)
    assert result.outcome != Outcome.HIT, (
        "a predicate fired while a line named in `none` was high"
    )
    assert result.visit(0).cause == "cancel"
    assert result.begin["cancel_reason"] == 4, "expected the trial cap to end it"


def test_a_predicate_already_true_on_entry_does_not_fire(device, loopback):
    """The rising-edge rule, which BRINGUP.md §3 warns looks like a fault.

    A transition fires on its predicate's *rising edge*, so a lever the animal
    is already holding must not end the trial the instant it begins. Input A is
    held high across the transition, so the predicate is true the moment state 1
    is entered -- and it must still wait for a change that never comes, ending
    on the cap.

    Holding the line high across the transition is the whole setup, and it takes
    both states raising it: a state lowers what it raised when it exits. An entry
    action in state 1 alone could not produce this condition at all, because the
    pin it drives is only read on the *next* scan -- that is a rising edge one
    scan after entry, which is precisely the case that should fire.
    """
    graph = GraphUpload(device.session, version=15)
    graph.begin(n_states=3, entry=0)
    graph.dist(0, kind="fixed", a=50)
    # State 0 raises output A and holds it for 50 ms.
    graph.state(0, terminal=None, timeout={"dist": 0, "target": 1})
    graph.action("entry", line=OUT_A, kind="high")
    # State 1 raises it again, so it never drops; input A is therefore already
    # high at the instant state 1 is entered, and the predicate already true.
    graph.state(1, terminal=None, timeout=None)
    graph.action("entry", line=OUT_A, kind="high")
    graph.transition(target=2, all=bit(IN_A))
    graph.state(2, terminal=int(Outcome.HIT), timeout=None)
    ok = graph.end()
    assert ok[Field.MSG_TYPE] == MsgType.SET_OK, ok

    result = run_trial(device, graph, trial_id=105, cap_ms=300)
    assert result.outcome != Outcome.HIT, (
        "a transition fired on a predicate that was already true when its state was entered"
    )


def test_level_makes_a_predicate_fire_on_entry(device, loopback):
    """`level: true` is the deliberate opt-out from the rising-edge rule.

    The same graph as above, one member different, so what is being tested is
    the flag and not the wiring: with `level` the already-true predicate fires
    immediately and the trial reaches HIT.
    """
    graph = GraphUpload(device.session, version=16)
    graph.begin(n_states=3, entry=0)
    graph.dist(0, kind="fixed", a=50)
    graph.state(0, terminal=None, timeout={"dist": 0, "target": 1})
    graph.action("entry", line=OUT_A, kind="high")
    graph.state(1, terminal=None, timeout=None)
    graph.action("entry", line=OUT_A, kind="high")  # held high, as above
    graph.transition(target=2, all=bit(IN_A), level=True)
    graph.state(2, terminal=int(Outcome.HIT), timeout=None)
    ok = graph.end()
    assert ok[Field.MSG_TYPE] == MsgType.SET_OK, ok

    result = run_trial(device, graph, trial_id=106, cap_ms=300)
    assert result.outcome == Outcome.HIT
    assert result.visit(1).cause == "transition"
    # It fired on the state's first evaluation, not after some delay.
    assert result.visit(1).duration_us < 5000, (
        f"a `level` transition took {result.visit(1).duration_us} us to fire on entry"
    )
