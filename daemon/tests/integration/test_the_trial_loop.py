# SPDX-License-Identifier: LGPL-3.0-or-later
"""The loop a rig runs, driven entirely through this daemon's own client.

Upload the session's graphs, subscribe, arm, start, and read what the daemon
published. Everything the unit suite asserts about conversions and refusal
tables is presupposed here; what only this can be wrong about is that the two
sides agree -- that the field names are the ones the daemon validates, that a
subscription opened before arming sees the result, and that the refusals arrive
with the codes the client's exception classes were built around.

**`statemachined-client`, over a real gRPC channel.** It was
`statemachined.client` over an in-process ASGI app; the routes are going and
the client is a distribution of its own now, so what this drives is the
published client against the published servicers.
"""

from __future__ import annotations

import json

import pytest
from rig_harness import timed_graph, wait_until
from statemachined_client import (
    KIND_TRIAL_RESULT,
    DaemonRefusedTheRequest,
    NoSuchDocument,
    TheRigIsNotInAStateForThat,
    TrialOutcome,
)

#: How long a trial gets to end before the suite calls it a failure. Generous
#: against a scan loop that is a nanosleep on a preemptible kernel, and finite
#: so a wedged daemon fails this in seconds rather than hanging CI.
TRIAL_DEADLINE_SECONDS = 10.0


def a_session_of(rig, *graphs):
    """Store some graphs and put them on the device, as a session does.

    The graph crosses as **text**, which is the shape the interface uses: the
    daemon is what parses and validates one, and a client carrying its own copy
    of those models would carry a copy that is right until it is not.
    """
    for graph in graphs:
        rig.write_graph(graph["name"], json.dumps(graph))
    return rig.upload_graph_set([graph["name"] for graph in graphs])


def outcomes_in(entries) -> list[str]:
    """The outcome names in a trial's published entries, in order.

    The trace carries a *name* — the word the device used — where
    `read_trial_result` carries the `.tdr` code as an enum. Both are asserted
    in this suite deliberately: they are two spellings of one fact and the day
    they disagree is a day somebody mislabels a dataset.
    """
    return [entry.payload["outcome"] for entry in entries if entry.kind == KIND_TRIAL_RESULT]


def test_one_whole_trial_from_upload_to_outcome(rig):
    uploaded = a_session_of(rig, timed_graph("go-nogo", outcome="HIT"))
    assert uploaded.slots == {"go-nogo": 0}

    armed = rig.configure_trial(193, graph="go-nogo", cap_milliseconds=5000)
    assert armed.graph == "go-nogo"
    assert armed.set_version == uploaded.set_version

    rig.start_trial(193)
    ended = rig.wait_for_trial(193, timeout_s=TRIAL_DEADLINE_SECONDS)
    assert ended.payload["outcome"] == "HIT"

    published = rig.read_trial_trace(193)
    assert outcomes_in(published) == ["HIT"], "one trial ends once"

    # And the same trial, read back into names against the graph that actually
    # ran rather than against whatever the store holds today.
    last = rig.read_trial_result()
    assert last.trial_id == 193
    assert last.outcome is TrialOutcome.HIT
    assert last.visits[0].state_name == "Wait"


def test_the_outcome_the_device_named_is_the_one_that_comes_back(rig):
    """The device treats the outcome code as opaque; the name is what crosses.

    Varying it is the only thing about a trial worth varying here -- the device
    cannot tell a HIT from a LATE and is not meant to.
    """
    a_session_of(rig, timed_graph("late", outcome="LATE"))
    rig.configure_trial(1, graph="late", cap_milliseconds=5000)
    rig.start_trial(1)
    rig.wait_for_trial(1, timeout_s=TRIAL_DEADLINE_SECONDS)

    assert rig.read_trial_result().outcome is TrialOutcome.LATE


def test_two_trials_keep_their_own_numbers_and_their_own_entries(rig):
    a_session_of(
        rig, timed_graph("one", outcome="HIT"), timed_graph("two", outcome="WRONG_RESPONSE")
    )

    for trial_id, graph in ((11, "one"), (12, "two")):
        rig.configure_trial(trial_id, graph=graph, cap_milliseconds=5000)
        rig.start_trial(trial_id)
        rig.wait_for_trial(trial_id, timeout_s=TRIAL_DEADLINE_SECONDS)

    # Every entry a trial's pull returns belongs to that trial. This is the
    # property the whole `trial_id` handshake exists for: an entry attributed to
    # the trial after the one it belongs to is how a rig mislabels a dataset.
    for trial_id, outcome in ((11, "HIT"), (12, "WRONG_RESPONSE")):
        entries = rig.read_trial_trace(trial_id)
        assert {entry.trial_id for entry in entries} == {trial_id}
        assert outcomes_in(entries) == [outcome]


def test_a_dropped_subscription_costs_nothing_because_the_pull_answers_exactly(rig):
    """The recovery path, exercised as a recovery rather than described.

    Nothing subscribes at all here. The trial runs, the daemon publishes, and
    the whole of it is still retrievable by id -- which is why a lost
    subscription is something a caller can recover from rather than a session
    it has lost.
    """
    a_session_of(rig, timed_graph("alone", outcome="HIT"))
    rig.configure_trial(1, graph="alone", cap_milliseconds=5000)
    rig.start_trial(1)

    assert wait_until(lambda: outcomes_in(rig.read_trial_trace(1)) or None) == ["HIT"]
    assert rig.read_observers().count == 0, "nothing was watching, and it did not matter"


def test_a_cancel_that_races_a_terminal_state_reports_the_real_outcome(rig):
    """Asking to cancel and being told HIT is the caller's to cope with.

    The alternative is a record claiming a trial was cancelled when the animal
    had already responded, and this client passes the answer through unchanged
    rather than fabricating one.
    """
    a_session_of(rig, timed_graph("quick", 1, outcome="HIT"))
    rig.configure_trial(1, graph="quick", cap_milliseconds=5000)
    rig.start_trial(1)
    rig.wait_for_trial(1, timeout_s=TRIAL_DEADLINE_SECONDS)

    # By now it has ended on its own. The cancel is refused or answers with the
    # outcome; either way nothing invents a CANCELLED.
    try:
        rig.cancel_trial(1)
    except DaemonRefusedTheRequest:
        pass
    assert rig.read_trial_result().outcome is TrialOutcome.HIT


def test_a_long_trial_can_be_cancelled_and_the_valve_comes_down(rig):
    """Cancellation is a forced transition through the ordinary exit path.

    Which is the reason it is safe: every output the state raised is lowered by
    the same code that lowers it on any other transition, so a valve cannot be
    left open by a graph that forgot something.
    """
    a_session_of(rig, timed_graph("slow", 30_000, outcome="HIT"))
    rig.configure_trial(1, graph="slow", cap_milliseconds=60_000)
    rig.start_trial(1)
    rig.cancel_trial(1)
    rig.wait_for_trial(1, timeout_s=TRIAL_DEADLINE_SECONDS)

    result = rig.read_trial_result()
    assert result.trial_id == 1
    assert result.cancel_reason is not TrialOutcome  # a reason, not an outcome
    assert result.cancel_reason.value != "none"
    lamp = _output_named(rig, "ready_lamp")
    assert lamp.is_high_now is False, "the state raised it; the exit path lowered it"


def _output_named(rig, name: str):
    return next(line for line in rig.read_lines().output_lines if line.name == name)


# ------------------------------------------------------------- the refusals ---


def test_a_graph_outside_the_committed_set_is_refused_by_name(rig):
    """Why a caller need validate nothing about a graph name.

    It can afford that precisely because the daemon refuses a name it does not
    have, loudly and by name, instead of running whatever it had loaded.
    """
    a_session_of(rig, timed_graph("present", outcome="HIT"))

    with pytest.raises(DaemonRefusedTheRequest) as refused:
        rig.configure_trial(1, graph="not-in-the-set")

    assert refused.value.error == "graph_not_in_set"
    assert "not-in-the-set" in refused.value.detail
    assert refused.value.context == "graph"


def test_starting_a_trial_the_device_was_not_armed_for_is_refused(rig):
    # The local form of VStim's StartPermittable(): no trial may start that the
    # device was not confirmed configured for, or a session runs the previous
    # trial's parameters without anybody noticing.
    a_session_of(rig, timed_graph("armed", outcome="HIT"))
    rig.configure_trial(1, graph="armed")

    with pytest.raises(DaemonRefusedTheRequest):
        rig.start_trial(2)


def test_arming_with_no_set_committed_says_which_gap_it_is(rig):
    with pytest.raises(TheRigIsNotInAStateForThat) as refused:
        rig.configure_trial(1, graph="anything")
    assert refused.value.error in {"no_graph_set", "graph_not_in_set"}


def test_a_trial_that_never_ran_has_nothing_recorded(rig):
    """An empty list, and **not** a refusal, which is the change from HTTP.

    The route answered 404 `no_trace_for_trial`. The ring cannot tell "no such
    trial" from "a trial whose entries have been overwritten" -- both are the
    same observation -- so a refusal was claiming to know which, and an empty
    list says exactly what was seen.
    """
    assert rig.read_trial_trace(999) == []


def test_a_graph_that_is_not_in_the_store_is_still_a_refusal(rig):
    """The store *can* tell, so it does. The contrast with the trace is the
    point: a refusal is for a question with a definite negative answer."""
    with pytest.raises(NoSuchDocument) as refused:
        rig.read_graph("never-stored")
    assert refused.value.error == "no_such_graph"
