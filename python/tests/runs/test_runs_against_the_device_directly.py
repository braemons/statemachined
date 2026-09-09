# SPDX-License-Identifier: GPL-3.0-or-later
"""Whole runs through `StatemachinedDevice`, with no daemon in the picture.

One of the package's two ways to drive a rig, and the one a script that owns the
board uses: it opens the port, compiles the graphs, uploads the set, arms each
trial and names the results itself. There is no store, no trace ring, no HTTP
layer and nobody else watching.

Every test here has a counterpart in `test_runs_through_the_daemon.py` running
the same paradigm to the same assertion, which is the point of the pair: a
failure in one and not the other localises immediately to the layer that differs
rather than to the board. And every test runs against either far end -- see this
directory's `conftest.py`.
"""

from __future__ import annotations

import paradigms
import pytest
from paradigms import ALL, WITHOUT_A_HARNESS, Paradigm, a_session_of

from statemachined.graph_set_compiler import GraphSetCompilationError
from statemachined.model.graph_definition import GraphDefinition


def upload(device, *chosen: Paradigm, set_version: int = 1):
    """Put these paradigms on the board as one committed set."""
    return device.upload_graph_set(
        [GraphDefinition.model_validate(paradigm.document) for paradigm in chosen],
        set_version=set_version,
    )


def check(result, paradigm: Paradigm):
    """The whole of what a correct run of `paradigm` produced.

    Outcome *and* path, always both. An outcome alone cannot tell a response
    window answered by a lever from one that timed out into a state declaring
    the same code, and that is exactly the difference the loopback harness
    exists to make visible.
    """
    assert result.outcome.name == paradigm.outcome, (
        f"{paradigm.name}: expected {paradigm.outcome}, got {result.outcome.name} "
        f"after {[visit.state_name for visit in result.visits]} -- {paradigm.why}"
    )
    assert [visit.state_name for visit in result.visits] == paradigm.path, paradigm.name


# ------------------------------------------------------ one run at a time ---


@pytest.mark.parametrize("paradigm", WITHOUT_A_HARNESS, ids=lambda p: p.name)
def test_a_paradigm_needing_no_input_runs_and_comes_back_named(device, paradigm):
    """The runs a bare board can do, so this much passes with no wires at all."""
    upload(device, paradigm)
    check(device.run_trial_to_completion(1, paradigm.name,
                                         cap_milliseconds=paradigm.cap_milliseconds), paradigm)


@pytest.mark.parametrize("paradigm", ALL, ids=lambda p: p.name)
def test_every_paradigm_runs_and_comes_back_named(device, the_loopback_harness, paradigm):
    """The whole set, including the four that wait on a line.

    Those four are the ones that could not run in CI at all before the host
    build grew a software harness: the chain from a pin through the input
    conditioner to a transition firing had never been exercised anywhere except
    on silicon, so a regression in it would have reached a rig.
    """
    upload(device, paradigm)
    check(device.run_trial_to_completion(1, paradigm.name,
                                         cap_milliseconds=paradigm.cap_milliseconds), paradigm)


# ------------------------------------------------------------- many runs ---


def test_a_session_of_many_trials_keeps_every_result_with_its_own_trial(
    device, the_loopback_harness
):
    """Twelve trials over one committed set, each named against its own graph.

    Several runs rather than one, because these are the failures that need a
    second trial to exist: a result reassembled against the previously armed
    graph, a stale set version, a visit sequence that restarts, an armed trial
    id that is not cleared between runs. One trial passes all of them.
    """
    upload(device, *ALL)
    session = a_session_of(12)

    for trial_id, paradigm in enumerate(session, start=1):
        result = device.run_trial_to_completion(
            trial_id, paradigm.name, cap_milliseconds=paradigm.cap_milliseconds
        )
        assert result.trial_id == trial_id, "a result arrived under the wrong trial"
        check(result, paradigm)


def test_the_visit_stream_arrives_as_it_happens_and_agrees_with_the_result(
    device, the_loopback_harness
):
    """The unsolicited half, over several runs.

    A `visit` is published as each state is left; the result carries the same
    path at the end. Two encoders of one fact, so they are checked against each
    other -- and over four trials, because a sequence number that restarts per
    trial and one that runs for the session look identical after one.
    """
    observed: list[tuple[int, str]] = []
    device.on_state_visit = lambda visit: observed.append((visit.trial_id, visit.visit.state_name))

    upload(device, *ALL)
    for trial_id, paradigm in enumerate(a_session_of(4), start=1):
        before = len(observed)
        result = device.run_trial_to_completion(
            trial_id, paradigm.name, cap_milliseconds=paradigm.cap_milliseconds
        )
        check(result, paradigm)

        # Every state, terminal one included: a terminal state is published
        # as it is entered rather than as it is left, so the stream and the
        # result carry the same path and not one that is short by its ending.
        streamed = [name for identifier, name in observed[before:] if identifier == trial_id]
        assert streamed == paradigm.path, (
            f"{paradigm.name}: the stream said {streamed}, the result said {paradigm.path}"
        )


def test_a_second_upload_replaces_the_set_and_old_names_stop_resolving(device):
    """Switching paradigm sets mid-session, which a block design does.

    The failure this catches is a set version that goes stale on one side: a
    device still holding version 1 while the supervisor arms against version 2
    would run the wrong graph and name the result against the right one.
    """
    first, second = WITHOUT_A_HARNESS[0], ALL[-1]
    upload(device, first, set_version=1)
    check(device.run_trial_to_completion(1, first.name,
                                         cap_milliseconds=first.cap_milliseconds), first)

    upload(device, second, set_version=2)
    with pytest.raises(GraphSetCompilationError) as refused:
        device.configure_trial(2, first.name)
    assert first.name in str(refused.value)


def test_a_cancel_stops_a_run_and_lowers_what_the_state_raised(device):
    """Cancellation as a forced transition through the ordinary exit path.

    The claim in the README that a valve cannot be left open by a graph that
    forgot something, checked on whichever far end this is: the outputs a
    running state raised must be down once the cancel's result has come back.
    """
    long_run = Paradigm(
        "a-long-hold",
        {
            "name": "a-long-hold",
            "entry": "Hold",
            "distributions": {"forever": {"kind": "fixed", "duration_ms": 30000}},
            "states": [
                {
                    "name": "Hold",
                    "on_entry": [{"line": "reward_valve", "kind": "high"}],
                    "on_exit": [{"line": "reward_valve", "kind": "low"}],
                    "timeout": {"after": "forever", "goto": "Done"},
                },
                {"name": "Done", "outcome": "HIT"},
            ],
        },
        outcome="CANCELLED",
        path=["Hold"],
        needs_loopback=False,
    )
    upload(device, long_run)
    device.configure_trial(1, long_run.name, cap_milliseconds=60000)
    device.start_trial(1)
    device.cancel_trial(1)

    result = device.wait_for_trial_result(timeout=10.0)
    assert result.outcome.name == "CANCELLED"
    assert result.cancel_reason.name == "HOST"

    valve = paradigms.LOOPBACK_LINE_MAP["output_lines"][2]["line_index"]
    assert not device.read_state_report()["io"]["out"] & (1 << valve), (
        "the valve was still open after the trial that opened it was cancelled"
    )
