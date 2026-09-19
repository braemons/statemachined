# SPDX-License-Identifier: GPL-3.0-or-later
"""The same runs, driven through the daemon's HTTP API.

The counterpart of `test_runs_against_the_device_directly.py`: same paradigms,
same assertions, same far end, and everything in between is different. Here a
graph is a document in a store, a set is uploaded by naming graphs, a trial is
armed over HTTP, and the result is read back from a trace ring that outlives the
caller.

That pairing is the tier's whole method. When a run passes directly and fails
here, the board is fine and the daemon is not -- the compiler, a route, the
trace, the store. When both fail, the far end is the suspect. Neither suite can
say that on its own, which is why duplicating the paradigms across them would
have defeated the point and they share one definition instead.

**What is checked through here and not through the device.** The trace, which
only a daemon has: a direct connection is the only listener there was, and what
it did not keep is gone. So the pull-vs-stream agreement, the recordings and the
per-trial query live in this file alone.
"""

from __future__ import annotations

import pytest
from paradigms import ALL, WITHOUT_A_HARNESS, a_session_of

from statemachined.client import Conflict


def upload(rig, *chosen):
    """Write these paradigms into the store and commit them as the session's set."""
    for paradigm in chosen:
        rig.graphs.write(paradigm.document)
    return rig.session.upload_graph_set([paradigm.name for paradigm in chosen])


def run(rig, trial_id: int, paradigm, timeout_seconds: float = 20.0):
    """Arm, start, wait for it to end, and hand back the named result.

    Subscribed *before* arming, which is the client's own rule and not a
    nicety: a subscription opened afterwards starts at the newest entry, and
    the shortest paradigm here is 40 ms. Reading `trial.result()` without
    waiting at all is the same bug one step further on -- it answers
    `no_result_yet` about a trial that is merely still running.
    """
    with rig.trace.subscribe(f"run-{trial_id}") as stream:
        rig.trial.configure(trial_id, graph=paradigm.name,
                            cap_milliseconds=paradigm.cap_milliseconds)
        rig.trial.start(trial_id)
        assert stream.wait_for_trial(trial_id, timeout_seconds=timeout_seconds), (
            f"trial {trial_id} ({paradigm.name}) never finished"
        )
    return rig.trial.result()


def check(result, paradigm):
    """The same two assertions the direct suite makes, on the daemon's shapes."""
    assert result["outcome"] == paradigm.outcome, (
        f"{paradigm.name}: expected {paradigm.outcome}, got {result['outcome']} "
        f"after {[visit['state_name'] for visit in result['visits']]} -- {paradigm.why}"
    )
    assert [visit["state_name"] for visit in result["visits"]] == paradigm.path, paradigm.name


# ------------------------------------------------------ one run at a time ---


@pytest.mark.parametrize("paradigm", WITHOUT_A_HARNESS, ids=lambda p: p.name)
def test_a_paradigm_needing_no_input_runs_through_the_api(rig, paradigm):
    upload(rig, paradigm)
    check(run(rig, 1, paradigm), paradigm)


@pytest.mark.parametrize("paradigm", ALL, ids=lambda p: p.name)
def test_every_paradigm_runs_through_the_api(rig, the_loopback_harness_over_the_api, paradigm):
    """The whole set over HTTP, including the four that wait on a line.

    Note what the daemon adds and the device does not: the graph here is a
    *document*, compiled by `graph_set_compiler` from names to line numbers
    against the wiring the daemon pushed. A paradigm that ran directly and fails
    here has found that compilation, not the engine.
    """
    upload(rig, paradigm)
    check(run(rig, 1, paradigm), paradigm)


# ------------------------------------------------------------- many runs ---


def test_a_session_of_many_trials_keeps_every_result_with_its_own_trial(
    rig, the_loopback_harness_over_the_api
):
    """Twelve trials over one committed set, over HTTP, each with its own entry.

    The same claim the direct suite makes, plus one only the daemon can be
    wrong about: `trace.for_trial(id)` must answer about *that* trial and no
    other, across a ring that has had a dozen trials through it.
    """
    upload(rig, *ALL)
    session = a_session_of(12)

    with rig.trace.subscribe("the-runs-suite") as stream:
        for trial_id, paradigm in enumerate(session, start=1):
            rig.trial.configure(trial_id, graph=paradigm.name,
                                cap_milliseconds=paradigm.cap_milliseconds)
            rig.trial.start(trial_id)
            assert stream.wait_for_trial(trial_id, timeout_seconds=20), f"trial {trial_id}"
            check(rig.trial.result(), paradigm)

    for trial_id, paradigm in enumerate(session, start=1):
        published = rig.trace.for_trial(trial_id)
        outcomes = [entry["outcome"] for entry in published if entry["kind"] == "trial_result"]
        assert outcomes == [paradigm.outcome], f"trial {trial_id} ({paradigm.name})"


def test_a_recording_holds_every_run_of_a_session(rig, the_loopback_harness_over_the_api):
    """What a session is *for*, from the daemon's side.

    A recording is the daemon's reason to exist -- it outlives the script that
    armed the trials, so the run is evidence rather than something that scrolled
    past. Checked over several trials because a recording that captured only the
    first would look identical after one.
    """
    upload(rig, *ALL)
    session = a_session_of(6)

    rig.recordings.start("a-session-of-runs")
    with rig.trace.subscribe("the-runs-suite") as stream:
        for trial_id, paradigm in enumerate(session, start=1):
            rig.trial.configure(trial_id, graph=paradigm.name,
                                cap_milliseconds=paradigm.cap_milliseconds)
            rig.trial.start(trial_id)
            assert stream.wait_for_trial(trial_id, timeout_seconds=20), f"trial {trial_id}"
    assert rig.recordings.stop()["state"] == "stopped"

    recording = rig.recordings.read("a-session-of-runs")
    assert recording["kind_counts"]["trial_result"] == len(session)


def test_the_stream_and_the_pull_agree_over_a_whole_session(
    rig, the_loopback_harness_over_the_api
):
    """A dropped subscription costs nothing, checked across many trials.

    The client's own claim: `trace.for_trial(id)` answers exactly what the
    stream did, which is what makes a lost WebSocket an exception to catch
    rather than a session to abandon. Read off the stream entry by entry rather
    than through `wait_for_trial`, because that one answers *whether* a trial
    ended and this test is about *what it said*.
    """
    upload(rig, *ALL)
    session = a_session_of(5)

    streamed: dict[int, list[str]] = {}
    with rig.trace.subscribe("the-runs-suite") as stream:
        published = stream.entries(timeout_seconds=20)
        for trial_id, paradigm in enumerate(session, start=1):
            rig.trial.configure(trial_id, graph=paradigm.name,
                                cap_milliseconds=paradigm.cap_milliseconds)
            rig.trial.start(trial_id)
            for entry in published:
                if entry.get("kind") == "trial_result" and entry["trial_id"] == trial_id:
                    streamed[trial_id] = [entry["outcome"]]
                    break
            assert trial_id in streamed, f"trial {trial_id} never published a result"

    assert len(streamed) == len(session)
    for trial_id, paradigm in enumerate(session, start=1):
        pulled = [
            entry["outcome"]
            for entry in rig.trace.for_trial(trial_id)
            if entry["kind"] == "trial_result"
        ]
        assert pulled == streamed[trial_id] == [paradigm.outcome], f"trial {trial_id}"


def test_switching_the_committed_set_mid_session_refuses_the_old_names(rig):
    """A block design changing paradigm sets, and the refusal that names it.

    Over HTTP the refusal is the daemon's rather than the compiler's, and it
    must arrive as a `Conflict` a caller can catch -- not as a trial armed
    against a graph the board no longer holds.
    """
    first, second = WITHOUT_A_HARNESS[0], ALL[-1]
    upload(rig, first)
    check(run(rig, 1, first), first)

    upload(rig, second)
    with pytest.raises(Conflict):
        rig.trial.configure(2, graph=first.name, cap_milliseconds=first.cap_milliseconds)


def test_the_wiring_the_daemon_pushed_is_what_the_paradigms_compile_against(rig):
    """Names to line numbers, resolved against the board rather than assumed.

    The daemon pushed this map at startup from the state-machine config, and the
    board answered which pin each line is. That round trip is what makes
    `cue_lamp` mean output line 0 in a graph -- and, with the harness on, what
    makes it drive `lever_left`.
    """
    lines = rig.device.lines()
    outputs = {line["name"]: line["line_index"] for line in lines["output_lines"]}
    inputs = {line["name"]: line["line_index"] for line in lines["input_lines"]}

    from paradigms import DRIVES

    for output_name, input_name in DRIVES.items():
        assert (inputs[input_name] - outputs[output_name]) % 8 == 4, (
            f"{output_name} is output line {outputs[output_name]} and {input_name} is "
            f"input line {inputs[input_name]}, which the loopback harness does not connect"
        )
