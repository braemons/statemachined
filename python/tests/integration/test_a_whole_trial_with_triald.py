# SPDX-License-Identifier: GPL-3.0-or-later
"""One trial, end to end: triald drives, this daemon runs it, triald observes.

**The loop a rig actually runs, and the only test of the handover.** Everything
else in this suite stops at this daemon's own API. What only this can be wrong
about is that the two halves fit: that a graph triald names is one this daemon
has, that a trial triald starts is the trial that runs, and that what this
daemon *publishes* is something triald can read and act on.

**Note the direction.** This daemon knows nothing about triald -- no client, no
base URL, no schema, no outbound call of any kind. It writes what it did to its
trace and assumes nobody read it. triald opens `WS /api/trace/stream`, which is
the whole of subscribing, and drives everything: it picks the trial, arms the
executor, starts it, sees the result go by, pulls that trial's events by id and
turns them into an outcome. Its `?observer=triald` is a label on this daemon's
diagnostics page and grants nothing.

That direction is why the test lives here and not in triald: the fixture that is
expensive is the firmware built for this machine, and it is here. The dependency
is test-only and one way, which is the same shape the coupling has anyway --
triald knows what an executor is, and an executor knows nothing.

**What is not covered.** The device's input lines: this daemon sends commands
and never drives inputs, so a graph waiting for a lever cannot be answered from
this process. These graphs reach their outcome on a timeout instead, which
exercises everything above the trigger. Driving lines is `tests/hardware/`, with
jumper wires.
"""

from __future__ import annotations

import pytest
from rig_harness import configuration_for, timed_graph, wait_until
from fastapi.testclient import TestClient
from statemachined.daemon.api.application import create_application

pytest.importorskip(
    "triald.executor",
    reason=(
        "triald is not installed: it is the `e2e` dependency group, which is "
        "separate because triald is a private repo and needs credentials to "
        "fetch. Run `make test-e2e`. Needs Python 3.12; this daemon runs on 3.11 "
        "too, and there these tests always skip."
    ),
)

from triald.api.app import create_app  # noqa: E402
from triald.api.statemachine_executor import StateMachineExecutor  # noqa: E402
from triald.executor import ExecutorError, TrialConfiguration  # noqa: E402

RIG_URL = "http://statemachined.test"


def graph_ending_on(name: str, outcome: str, milliseconds: int = 40) -> dict:
    """A graph that waits and then declares `outcome`, with nothing to press.

    The outcome is the whole variable: the device cannot tell a HIT from a LATE
    and is not meant to, so what this varies is the only thing about a trial
    this daemon publishes and triald judges.
    """
    graph = timed_graph(name, milliseconds)
    graph["states"] = [
        {
            "name": "Wait",
            "on_entry": [{"line": "ready_lamp", "kind": "high"}],
            "timeout": {"after": "dwell", "goto": "Done"},
        },
        {"name": "Done", "outcome": outcome},
    ]
    return graph


@pytest.fixture
def rig(native_device, tmp_path):
    """This daemon, in front of the firmware built for this machine.

    Note what `configuration_for` no longer takes: there is no setting here
    naming triald, or anything else. This daemon is configured with a device and
    some directories.
    """
    application = create_application(configuration_for(native_device, tmp_path))
    with TestClient(application) as client:
        yield client


@pytest.fixture
def executor(rig):
    """triald's client for that rig, over the rig's own ASGI app.

    `TestClient` routes by path and ignores the host, so triald builds exactly
    the URL it would build on a real network.
    """
    return StateMachineExecutor(RIG_URL, client=rig)


@pytest.fixture
def triald(rig):
    """A real triald, in this process, with its demo experiment armed."""
    with TestClient(create_app()) as client:
        armed = client.post("/api/session/arm")
        assert armed.status_code == 200, armed.text
        yield client


def run_one_trial(rig, triald, executor, *, outcome="HIT", graph="e2e") -> dict:
    """The rig's loop, in the order a rig runs it.

    triald decides which trial this is and hands out its number; the executor is
    armed for that number and nothing else; the executor publishes; triald reads
    what it published and reports the outcome to itself.
    """
    rig.put(f"/api/graphs/{graph}", json=graph_ending_on(graph, outcome))
    rig.post("/api/session/graphs", json={"graph_names": [graph]})

    spec = triald.post("/api/trial/next").json()
    trial_id = spec["trial_number"]

    executor.configure(
        TrialConfiguration(
            trial_id=trial_id,
            statemachine_graph=spec["statemachine_graph"] or graph,
            cap_milliseconds=5000,
        )
    )
    executor.start(trial_id)

    # Observed, not waited on by the executor: the daemon published and moved on.
    with rig.websocket_connect("/api/trace/stream?observer=triald") as stream:
        finished = next(executor.finished_trials(iter(lambda: stream.receive_text(), None)))
    assert finished == trial_id

    report = executor.outcome_of(trial_id)
    return triald.post(
        "/api/trial/outcome",
        json={"trial_id": trial_id, **_wire(report)},
    ).json()


def _wire(report) -> dict:
    """triald's own report, as its API takes it.

    In a running daemon this is a call into the session rather than a round trip
    over HTTP. It goes over the API here so the test asserts on the same
    validation a rig's other clients meet.
    """
    return {
        "outcome": report.outcome.name,
        "manipulandum": report.manipulandum.name,
        "reaction_time_ms": report.reaction_time_ms,
        "terminating_interval": report.terminating_interval,
        "precise_fixation": report.precise_fixation,
        "reward_ms": report.reward_ms,
        "simulated": report.simulated,
        "note": report.note,
    }


# ---------------------------------------------------------- the whole loop ---


def test_a_trial_triald_started_comes_back_in_trialds_record(rig, triald, executor):
    record = run_one_trial(rig, triald, executor, outcome="HIT")

    assert record["outcome"]["code"] == 1
    assert record["outcome"]["name"] == "HIT"
    assert record["accepted"] is True
    assert record["refusal_reason"] is None

    totals = triald.get("/api/state").json()["totals"]
    assert totals["total"] == 1
    assert totals["hits"] == 1
    assert totals["accepted"] == 1


def test_the_outcome_the_device_named_is_the_one_triald_counts(rig, triald, executor):
    # The device treats the code as opaque and triald owns what it means. This
    # is the assertion that the eleven .tdr outcomes are spelled the same on
    # both sides -- the name is what crosses, not the number.
    record = run_one_trial(rig, triald, executor, outcome="UNEXPECTED_START_SIGNAL")
    assert record["outcome"]["code"] == 8
    assert record["outcome"]["name"] == "UNEXPECTED_START_SIGNAL"


def test_the_record_says_the_outcome_was_not_simulated(rig, triald, executor):
    # `triald sim` produces outcomes with simulated: true, and a rig whose
    # records could not be told apart from a simulator's is a rig whose data
    # cannot be trusted.
    assert run_one_trial(rig, triald, executor)["outcome"]["simulated"] is False


def test_the_veto_fields_nobody_here_can_observe_are_left_alone(rig, triald, executor):
    # precise_fixation comes from an eye monitor, frame_loss from vstimd, and
    # either can veto acceptance on its own. Neither daemon in this test has
    # heard of either.
    record = run_one_trial(rig, triald, executor)
    assert record["outcome"]["precise_fixation"] is True
    assert record["outcome"]["frame_loss"] is None


def test_two_trials_keep_their_own_numbers(rig, triald, executor):
    first = run_one_trial(rig, triald, executor, graph="e2e-one")
    second = run_one_trial(rig, triald, executor, graph="e2e-two")

    assert second["trial"]["trial_number"] == first["trial"]["trial_number"] + 1
    assert triald.get("/api/state").json()["totals"]["total"] == 2


# ------------------------------------------------------- the refusals ---


def test_a_graph_the_executor_does_not_have_is_refused_by_name(rig, triald, executor):
    """Why triald validates nothing about a graph name.

    triald holds no graphs and has no opinion about where one sits in the
    executor's store. It can afford that precisely because the executor refuses
    a name it does not have, loudly and by name, instead of running whatever it
    had loaded.
    """
    rig.put("/api/graphs/present", json=graph_ending_on("present", "HIT"))
    rig.post("/api/session/graphs", json={"graph_names": ["present"]})
    trial_id = triald.post("/api/trial/next").json()["trial_number"]

    with pytest.raises(ExecutorError) as refused:
        executor.configure(
            TrialConfiguration(trial_id=trial_id, statemachine_graph="not-in-the-set")
        )
    assert "not-in-the-set" in str(refused.value)
    assert "graph_not_in_set" in str(refused.value)


def test_starting_a_trial_the_executor_was_not_armed_for_is_refused(rig, triald, executor):
    # The local form of StartPermittable(): no trial may start that the executor
    # was not confirmed configured for, or a session runs the previous trial's
    # parameters without anybody noticing.
    rig.put("/api/graphs/armed", json=graph_ending_on("armed", "HIT"))
    rig.post("/api/session/graphs", json={"graph_names": ["armed"]})
    executor.configure(TrialConfiguration(trial_id=1, statemachine_graph="armed"))

    with pytest.raises(ExecutorError):
        executor.start(2)


def test_an_outcome_for_the_wrong_trial_is_refused_by_triald(rig, triald, executor):
    """The failure the whole handshake exists to prevent.

    A report attributed to the trial *after* the one it belongs to is how a rig
    quietly mislabels a dataset. triald refuses rather than guessing -- it
    cannot tell which of the two is the truth, so it takes neither.
    """
    run_one_trial(rig, triald, executor)
    in_flight = triald.post("/api/trial/next").json()["trial_number"]

    refused = triald.post(
        "/api/trial/outcome", json={"trial_id": in_flight + 7, "outcome": "HIT"}
    )
    assert refused.status_code == 409

    state = triald.get("/api/state").json()
    assert state["current"]["trial_number"] == in_flight
    assert state["totals"]["total"] == 1


def test_asking_for_a_trial_the_executor_never_ran_says_so(rig, executor):
    """And it says it in *this* daemon's words, not in triald's paraphrase.

    The message used to be triald's own -- it held a copy of this API's refusal
    shape. It now comes through `statemachined.client`, so what reaches the
    session log is the code this daemon documents (`no_trace_for_trial`) and the
    field it names. That is the point of the client: one description of this
    API, shipped from here, rather than a second one maintained over there.
    """
    with pytest.raises(ExecutorError) as refused:
        executor.outcome_of(999)

    assert "no_trace_for_trial" in str(refused.value)
    assert "999" in str(refused.value)


# ---------------------------------------------- the rig with nobody watching ---


def test_the_daemon_runs_a_trial_with_nothing_observing_it(rig):
    """The counterpart to everything above, and the point of the direction.

    No triald, no subscriber, nobody. The trial runs, the result is published,
    and it stays on the rig. This is what a bench box looks like every day, and
    it is a supported configuration rather than a degraded one.
    """
    rig.put("/api/graphs/alone", json=graph_ending_on("alone", "HIT"))
    rig.post("/api/session/graphs", json={"graph_names": ["alone"]})
    rig.post("/api/trial/configure", json={"trial_id": 1, "graph": "alone"})
    rig.post("/api/trial/start", json={"trial_id": 1})

    result = wait_until(
        lambda: rig.get("/api/trial/result").json()
        if rig.get("/api/trial/result").status_code == 200
        else None
    )
    assert result is not None
    assert result["outcome"] == "HIT"
    assert rig.get("/api/observers").json()["count"] == 0
