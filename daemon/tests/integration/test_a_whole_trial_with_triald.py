# SPDX-License-Identifier: GPL-3.0-or-later
"""One trial, end to end: triald, this daemon, and the firmware.

**The loop a rig actually runs, and the first test of it.** Everything else in
this suite stops at the daemon's own API. What only this can be wrong about is
the handover: that the outcome this daemon posts is one triald accepts, that it
is attributed to the trial it belongs to, and that triald's acceptance rules
then see the modifiers the device could and could not supply.

That handover was broken and nothing caught it. This daemon sent `trial_id`,
triald's schema forbade unknown fields and had no such field, and every trial's
outcome came back 422 -- while `tests/unit/test_triald_client.py` asserted the
field against an `httpx.MockTransport` that answers 200 to anything. A mock at
the far end of a contract tests one side's opinion of the contract twice.
Contracts INTERACTIONS.md §5.1, and §8's stage 1.

**Why triald is in-process.** It is a pip-installable FastAPI app, and
Starlette's `TestClient` is an `httpx.Client` over one -- so the daemon's own
`TrialdClient` can be handed it and reach the real thing with no subprocess, no
port and no teardown race. Same routing, same validation, same session state a
rig gets. What carries the bytes is the *only* thing replaced, and it is
replaced with triald.

**What is not covered here.** The device's input lines: the daemon sends
commands and never drives inputs, so a graph that waits for a lever cannot be
answered from this process. These graphs reach their outcome on a timeout
instead, which exercises everything above the trigger. Driving lines is
`tests/hardware/`, with jumper wires.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from statemachined.api.application import create_application
from statemachined.triald_client import TrialdClient

from conftest import configuration_for, timed_graph, wait_until

triald_app = pytest.importorskip(
    "triald.api.app",
    reason=(
        "triald is not installed: it is the `e2e` dependency group, which is "
        "separate because triald is a private repo and needs credentials to "
        "fetch. Run `make test-e2e`. Needs Python 3.12; this daemon runs on 3.11 "
        "too, and there these tests always skip."
    ),
)

TRIALD_URL = "http://triald.test"


def graph_ending_on(name: str, outcome: str, milliseconds: int = 40) -> dict:
    """A graph that waits and then declares `outcome`, with nothing to press.

    The outcome is the whole variable: the device cannot tell a HIT from a LATE
    and is not meant to, so what this varies is the only thing about a trial
    this daemon reports and triald judges.
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
def triald_client():
    """A real triald, mounted in this process, with its demo experiment armed."""
    application = triald_app.create_app()
    with TestClient(application) as client:
        armed = client.post("/api/session/arm")
        assert armed.status_code == 200, armed.text
        yield client


@pytest.fixture
def rig(native_device, tmp_path, triald_client):
    """This daemon, wired to that triald, over that triald's own ASGI app.

    `configuration_for` gives the daemon its device, its stores and its trace;
    the only thing replaced afterwards is what carries the one outbound call.
    """
    configuration = configuration_for(native_device, tmp_path, triald_base_url=TRIALD_URL)
    application = create_application(configuration)
    with TestClient(application) as client:
        service = application.state.rig_service
        # `TestClient` routes by path and ignores the host, so the daemon still
        # builds the URL it would build on a rig.
        service.triald = TrialdClient(TRIALD_URL, client=triald_client)
        yield client


def run_one_trial(rig, triald_client, *, outcome: str = "HIT", graph: str = "e2e") -> dict:
    """Select a trial in triald, run it on the device, and give triald's record.

    The order is the rig's: triald decides *which* trial this is and hands out
    its number; the device is configured for that number and nothing else; the
    outcome comes back addressed to it.
    """
    rig.put(f"/api/graphs/{graph}", json=graph_ending_on(graph, outcome))
    rig.post("/api/session/graphs", json={"graph_names": [graph]})

    spec = triald_client.post("/api/trial/next").json()
    trial_id = spec["trial_number"]

    armed = rig.post(
        "/api/trial/configure",
        json={"trial_id": trial_id, "graph": graph, "cap_milliseconds": 5000},
    )
    assert armed.status_code == 200, armed.text
    assert rig.post("/api/trial/start", json={"trial_id": trial_id}).status_code == 200

    # The daemon posts to triald from the link thread the moment the result
    # arrives, so what is waited on is triald's record, not the device's. And it
    # is waited on *by number*: `last` already holds the previous trial, so
    # waiting for it to be non-null would pass before this trial ended.
    def this_trials_record():
        last = triald_client.get("/api/state").json()["last"]
        return last if last and last["trial"]["trial_number"] == trial_id else None

    record = wait_until(this_trials_record)
    assert record is not None, "the outcome never reached triald"
    return record


# ---------------------------------------------------------- the whole loop ---


def test_a_trial_this_daemon_ran_arrives_in_trialds_record(rig, triald_client):
    record = run_one_trial(rig, triald_client, outcome="HIT")

    assert record["outcome"]["code"] == 1
    assert record["outcome"]["name"] == "HIT"
    assert record["accepted"] is True
    assert record["refusal_reason"] is None

    # And it counted, which is the half a 422 would have left untouched.
    totals = triald_client.get("/api/state").json()["totals"]
    assert totals["total"] == 1
    assert totals["hits"] == 1
    assert totals["accepted"] == 1


def test_the_record_says_the_outcome_was_not_simulated(rig, triald_client):
    # `triald sim` produces outcomes with simulated: true, and a rig whose
    # records could not be told apart from a simulator's is a rig whose data
    # cannot be trusted. This daemon sends false, always.
    record = run_one_trial(rig, triald_client)
    assert record["outcome"]["simulated"] is False


def test_an_outcome_the_device_named_is_the_one_triald_counts(rig, triald_client):
    # The device treats the code as opaque and triald owns what it means. This
    # is the one assertion that the eleven .tdr codes are spelled the same on
    # both sides -- code 8 was not, and the wire carries the *name*.
    record = run_one_trial(rig, triald_client, outcome="INEXPECTED_START_SIGNAL")
    assert record["outcome"]["code"] == 8
    assert record["outcome"]["name"] == "INEXPECTED_START_SIGNAL"


def test_the_veto_fields_the_device_cannot_know_are_left_for_others(rig, triald_client):
    # This daemon has never heard of the eye monitor or of vstimd, so it sends
    # neither modifier and triald takes its own defaults. Filling one in with a
    # plausible value would make the daemon a second decision authority.
    record = run_one_trial(rig, triald_client)
    assert record["outcome"]["precise_fixation"] is True
    assert record["outcome"]["frame_loss"] is None


def test_the_reaction_time_is_absent_when_nothing_responded(rig, triald_client):
    # These graphs end on a timeout: there was no response to time. Zero rather
    # than the duration of whatever state happened to be last.
    record = run_one_trial(rig, triald_client)
    assert record["outcome"]["reaction_time_ms"] == 0


def test_two_trials_keep_their_own_numbers(rig, triald_client):
    first = run_one_trial(rig, triald_client, graph="e2e-one")
    second = run_one_trial(rig, triald_client, graph="e2e-two")

    assert second["trial"]["trial_number"] == first["trial"]["trial_number"] + 1
    assert triald_client.get("/api/state").json()["totals"]["total"] == 2


# -------------------------------------------------- the negative that matters ---


def test_an_outcome_for_the_wrong_trial_is_refused_by_triald(rig, triald_client):
    """The failure this whole handshake exists to prevent.

    A report that arrives late is attributed to the trial *after* the one it
    belongs to, and the dataset is quietly mislabelled. triald refuses rather
    than guessing, and this asserts the refusal reaches the caller as an error
    instead of being swallowed into a false success.
    """
    run_one_trial(rig, triald_client)
    in_flight = triald_client.post("/api/trial/next").json()["trial_number"]

    stale = rig.app.state.rig_service.triald
    from statemachined.model.trial_outcome import TrialOutcome
    from statemachined.model.trial_record import TrialResultRecord

    with pytest.raises(httpx.HTTPStatusError) as refused:
        stale.report_trial_outcome(
            TrialResultRecord(
                trial_id=in_flight + 7,
                outcome=TrialOutcome.HIT,
                visits=[],
            )
        )
    assert refused.value.response.status_code == 409

    # And the trial triald is holding is untouched: still in flight, still uncounted.
    state = triald_client.get("/api/state").json()
    assert state["current"]["trial_number"] == in_flight
    assert state["totals"]["total"] == 1


def test_a_daemon_with_no_triald_still_runs_a_trial(native_device, tmp_path):
    """A bench box has no triald, and that is a configuration, not a fault.

    The counterpart to everything above: the same trial, the same device, and
    nowhere to send the outcome. It must run anyway, or the first thing anybody
    does with this package is the thing that fails.
    """
    configuration = configuration_for(native_device, tmp_path, triald_base_url="")
    with TestClient(create_application(configuration)) as rig:
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
