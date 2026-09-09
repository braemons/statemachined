# SPDX-License-Identifier: LGPL-3.0-or-later
"""The loop a rig runs, driven entirely through this client.

Upload the session's graphs, subscribe, arm, start, and read what the daemon
published. Everything the unit suite asserts about paths and bodies is
presupposed here; what only this can be wrong about is that the far end agrees
-- that the field names are the ones the daemon validates, that a subscription
opened before arming sees the result, and that the refusals arrive with the
codes the client's exception classes were built around.
"""

from __future__ import annotations

import pytest
from rig_harness import timed_graph, wait_until
from statemachined.client import Conflict, NotConnected, NotFound


def a_session_of(rig, *graphs) -> dict:
    """Store some graphs and put them on the device, as a session does."""
    for graph in graphs:
        rig.graphs.write(graph)
    return rig.session.upload_graph_set([graph["name"] for graph in graphs])


def test_one_whole_trial_from_upload_to_outcome(rig):
    uploaded = a_session_of(rig, timed_graph("go-nogo", outcome="HIT"))
    assert uploaded["slots"] == {"go-nogo": 0}

    # Subscribed *before* arming, which is the ordering the API's docstring
    # insists on: a subscription opened afterwards starts at the newest entry,
    # and a 40 ms trial ends before anybody is watching.
    with rig.trace.subscribe("the-client-tests") as stream:
        armed = rig.trial.configure(193, graph="go-nogo", cap_milliseconds=5000)
        assert armed["graph"] == "go-nogo"
        assert armed["set_version"] == uploaded["set_version"]

        rig.trial.start(193)
        assert stream.wait_for_trial(193, timeout_seconds=10) is True

    published = rig.trace.for_trial(193)
    result = [entry for entry in published if entry["kind"] == "trial_result"]
    assert len(result) == 1, "one trial ends once"
    assert result[0]["outcome"] == "HIT"

    # And the same trial, read back into names against the graph that actually
    # ran rather than against whatever the store holds today.
    last = rig.trial.result()
    assert last["trial_id"] == 193
    assert last["outcome"] == "HIT"
    assert next(visit["state_name"] for visit in last["visits"]) == "Wait"


def test_the_outcome_the_device_named_is_the_one_that_comes_back(rig):
    """The device treats the outcome code as opaque; the name is what crosses.

    Varying it is the only thing about a trial worth varying here -- the device
    cannot tell a HIT from a LATE and is not meant to.
    """
    a_session_of(rig, timed_graph("late", outcome="LATE"))
    with rig.trace.subscribe() as stream:
        rig.trial.configure(1, graph="late", cap_milliseconds=5000)
        rig.trial.start(1)
        assert stream.wait_for_trial(1, timeout_seconds=10)

    assert rig.trial.result()["outcome"] == "LATE"


def test_two_trials_keep_their_own_numbers_and_their_own_entries(rig):
    a_session_of(rig, timed_graph("one", outcome="HIT"), timed_graph("two", outcome="WRONG_RESPONSE"))

    with rig.trace.subscribe() as stream:
        for trial_id, graph in ((11, "one"), (12, "two")):
            rig.trial.configure(trial_id, graph=graph, cap_milliseconds=5000)
            rig.trial.start(trial_id)
            assert stream.wait_for_trial(trial_id, timeout_seconds=10)

    # Every entry a trial's pull returns belongs to that trial. This is the
    # property the whole `trial_id` handshake exists for: an entry attributed to
    # the trial after the one it belongs to is how a rig mislabels a dataset.
    for trial_id, outcome in ((11, "HIT"), (12, "WRONG_RESPONSE")):
        entries = rig.trace.for_trial(trial_id)
        assert {entry["trial_id"] for entry in entries} == {trial_id}
        assert [e["outcome"] for e in entries if e["kind"] == "trial_result"] == [outcome]


def test_a_dropped_subscription_costs_nothing_because_the_pull_answers_exactly(rig):
    """The recovery path, exercised as a recovery rather than described.

    Nothing subscribes at all here. The trial runs, the daemon publishes, and
    the whole of it is still retrievable by id -- which is why a lost
    subscription is an exception a caller can catch rather than a session it has
    lost.
    """
    a_session_of(rig, timed_graph("alone", outcome="HIT"))
    rig.trial.configure(1, graph="alone", cap_milliseconds=5000)
    rig.trial.start(1)

    assert wait_until(lambda: rig.trace.for_trial(1) if _has_result(rig, 1) else None)
    assert rig.trace.observers()["count"] == 0


def _has_result(rig, trial_id: int) -> bool:
    try:
        return any(entry["kind"] == "trial_result" for entry in rig.trace.for_trial(trial_id))
    except NotFound:
        return False


def test_a_cancel_that_races_a_terminal_state_reports_the_real_outcome(rig):
    """Asking to cancel and being told HIT is the caller's to cope with.

    The alternative is a record claiming a trial was cancelled when the animal
    had already responded, and this client passes the answer through unchanged
    rather than fabricating one.
    """
    a_session_of(rig, timed_graph("quick", 1, outcome="HIT"))
    with rig.trace.subscribe() as stream:
        rig.trial.configure(1, graph="quick", cap_milliseconds=5000)
        rig.trial.start(1)
        assert stream.wait_for_trial(1, timeout_seconds=10)

    # By now it has ended on its own. The cancel is refused or answers with the
    # outcome; either way nothing invents a CANCELLED.
    try:
        cancelled = rig.trial.cancel(1)
    except Conflict:
        pass
    else:
        assert cancelled["outcome_code"] is None or cancelled["cancelled"] in (True, False)
    assert rig.trial.result()["outcome"] == "HIT"


def test_a_long_trial_can_be_cancelled_and_the_valve_comes_down(rig):
    """Cancellation is a forced transition through the ordinary exit path.

    Which is the reason it is safe: every output the state raised is lowered by
    the same code that lowers it on any other transition, so a valve cannot be
    left open by a graph that forgot something.
    """
    a_session_of(rig, timed_graph("slow", 30_000, outcome="HIT"))
    with rig.trace.subscribe() as stream:
        rig.trial.configure(1, graph="slow", cap_milliseconds=60_000)
        rig.trial.start(1)
        rig.trial.cancel(1)
        assert stream.wait_for_trial(1, timeout_seconds=10)

    result = rig.trial.result()
    assert result["trial_id"] == 1
    assert result["cancel_reason"] != "NONE"
    lamp = _output_named(rig, "ready_lamp")
    assert lamp["is_high_now"] is False, "the state raised it; the exit path lowered it"


def _output_named(rig, name: str) -> dict:
    return next(line for line in rig.device.lines()["output_lines"] if line["name"] == name)


# ------------------------------------------------------------- the refusals ---


def test_a_graph_outside_the_committed_set_is_refused_by_name(rig):
    """Why a caller need validate nothing about a graph name.

    It can afford that precisely because the daemon refuses a name it does not
    have, loudly and by name, instead of running whatever it had loaded.
    """
    a_session_of(rig, timed_graph("present", outcome="HIT"))

    with pytest.raises(Conflict) as refused:
        rig.trial.configure(1, graph="not-in-the-set")

    assert refused.value.error == "graph_not_in_set"
    assert "not-in-the-set" in refused.value.detail
    assert refused.value.context == "graph"


def test_starting_a_trial_the_device_was_not_armed_for_is_refused(rig):
    # The local form of VStim's StartPermittable(): no trial may start that the
    # device was not confirmed configured for, or a session runs the previous
    # trial's parameters without anybody noticing.
    a_session_of(rig, timed_graph("armed", outcome="HIT"))
    rig.trial.configure(1, graph="armed")

    with pytest.raises(Conflict):
        rig.trial.start(2)


def test_arming_with_no_set_committed_says_which_gap_it_is(rig):
    with pytest.raises(Conflict) as refused:
        rig.trial.configure(1, graph="anything")
    assert refused.value.error in {"no_graph_set", "graph_not_in_set"}


def test_a_trial_that_never_ran_has_nothing_recorded(rig):
    with pytest.raises(NotFound) as refused:
        rig.trace.for_trial(999)
    assert refused.value.error == "no_trace_for_trial"


def test_a_daemon_with_no_device_refuses_the_loop_and_says_so(daemon, rig, native_device):
    """The refusal that is not about the request at all.

    503, its own class, and a message that says there is no board -- as opposed
    to a silence, which would mean the daemon itself is gone. The two have
    different recoveries and this is where the difference is real.
    """
    a_session_of(rig, timed_graph("gone", outcome="HIT"))
    native_device.stop()
    wait_until(lambda: not rig.health()["device_connected"], timeout_seconds=10)

    assert rig.health()["ok"] is True
    with pytest.raises(NotConnected):
        rig.trial.configure(1, graph="gone")
