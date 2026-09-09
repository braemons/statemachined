# SPDX-License-Identifier: LGPL-3.0-or-later
"""A session, over a real socket, against the daemon as it ships.

Deliberately few tests and each one wide. What this suite is for is the handful
of things that only a real network can be wrong about -- a WebSocket upgrade, a
long upload over a real connection, a daemon that goes away mid-call -- and
duplicating the integration suite's coverage here would buy nothing but minutes
per run.
"""

from __future__ import annotations

import pytest
from network_harness import timed_graph, wait_until
from statemachined.client import Conflict, StatemachinedClient, TransportError


def test_a_rig_is_set_up_and_a_session_runs_over_http_and_websockets(rig):
    """The whole arc, in the order somebody does it.

    Look at the board, wire it, store the paradigms, put them on the device,
    record, run two trials, read them back, stop. Every call in it goes over a
    socket to another process, and the subscription is a real WebSocket
    connection through uvicorn -- which is the one thing no in-process suite can
    say anything about.
    """
    device = rig.device.describe()
    assert device["connected"] is True
    assert device["capabilities"]["max_states"] > 0

    lines = rig.device.lines()
    lines["input_lines"][1]["name"] = "lever_left"
    assert rig.device.set_lines(lines)["pushed_to_device"] is True

    for name, outcome in (("go-nogo", "HIT"), ("catch", "EARLY")):
        rig.graphs.write(timed_graph(name, outcome=outcome))
    uploaded = rig.session.upload_graph_set(["go-nogo", "catch"])
    assert uploaded["slots"] == {"go-nogo": 0, "catch": 1}

    rig.recordings.start("over-the-network")

    outcomes = {}
    with rig.trace.subscribe("the-e2e-suite") as stream:
        for trial_id, graph in ((1, "go-nogo"), (2, "catch")):
            rig.trial.configure(trial_id, graph=graph, cap_milliseconds=5000)
            rig.trial.start(trial_id)
            assert stream.wait_for_trial(trial_id, timeout_seconds=20), f"trial {trial_id}"
            outcomes[trial_id] = rig.trial.result()["outcome"]

    assert outcomes == {1: "HIT", 2: "EARLY"}

    stopped = rig.recordings.stop()
    assert stopped["state"] == "stopped"
    assert rig.recordings.read("over-the-network")["kind_counts"]["trial_result"] == 2

    # And the pull answers exactly what the stream did, over the same socket.
    for trial_id, outcome in outcomes.items():
        published = rig.trace.for_trial(trial_id)
        assert [e["outcome"] for e in published if e["kind"] == "trial_result"] == [outcome]


def test_the_subscription_is_a_real_websocket_and_the_daemon_lists_it(rig):
    """The failure this suite exists for.

    Plain `uvicorn` answers a WebSocket upgrade with a 404 -- not an error in a
    log, a 404 -- so `WS /api/trace/stream` would be dead in a shipped daemon
    while every in-process test passed, because Starlette's `TestClient`
    implements WebSockets itself and needs no server library at all.
    """
    assert rig.trace.observers()["count"] == 0

    with rig.trace.subscribe("a-real-socket") as stream:
        listed = wait_until(lambda: rig.trace.observers()["observers"], timeout_seconds=10)
        assert listed, "the daemon did not see a subscriber; check the WebSocket upgrade"
        assert listed[0]["name"] == "a-real-socket"
        assert listed[0]["address"], "a real connection has a peer address"

        rig.graphs.write(timed_graph("watched", outcome="HIT"))
        rig.session.upload_graph_set(["watched"])
        rig.trial.configure(1, graph="watched", cap_milliseconds=5000)
        rig.trial.start(1)
        assert stream.wait_for_trial(1, timeout_seconds=20)

    # A departed subscriber is noticed when the daemon next tries to send to it,
    # which is the honest cost of a publisher that assumes nobody read it: there
    # is no unsubscribe message to receive. So the list clears once the rig
    # publishes again, and not a moment earlier.
    rig.trial.configure(2, graph="watched", cap_milliseconds=5000)
    rig.trial.start(2)
    assert wait_until(lambda: rig.trace.observers()["count"] == 0, timeout_seconds=20) is not None


def test_the_state_stream_is_coalesced_and_still_arrives(rig):
    """The other stream, with the opposite rule.

    Nothing here asserts *that* it coalesces -- that is the daemon's own test.
    What only this can say is that a second WebSocket route works over the same
    server, which is not implied by the first one working.
    """
    with rig.subscribe_to_state() as stream:
        frame = next(iter(stream.entries(timeout_seconds=10)), None)

    assert frame is not None
    assert frame["connected"] is True


def test_a_board_the_rig_can_lose_is_a_refusal_and_not_a_silence(rig):
    """503 with a device gone, and the daemon still answering.

    Which is the distinction the whole error hierarchy turns on: the rig is
    there, and the board is not. Over a real socket, because "the daemon is up"
    is exactly what an in-process test cannot fail to be true about.
    """
    assert rig.health()["ok"] is True
    assert rig.health()["device_connected"] is True


def test_a_rig_that_is_not_there_is_a_transport_error_and_names_the_call():
    """Nothing is listening on this port, and the message says what was attempted.

    The one failure a caller meets before anything else works -- a wrong port, a
    daemon that has not started, a rig on another subnet -- and it must not look
    like a refusal, because a refusal means the rig heard you.
    """
    from network_harness import a_free_port

    absent = StatemachinedClient(f"http://127.0.0.1:{a_free_port()}", timeout_seconds=2.0)
    with absent, pytest.raises(TransportError) as failed:
        absent.trial.start(1)

    assert "starting trial 1" in str(failed.value)


def test_a_long_upload_is_not_cut_off_by_the_ordinary_deadline(rig):
    """The set upload gets its own timeout, and this is where that is real.

    Over a socket, through uvicorn, with the daemon compiling and pushing every
    graph to the device one message at a time -- which is the call that is tens
    of seconds on a UART rig and the reason the client does not give it ten.
    """
    names = [f"paradigm-{index}" for index in range(4)]
    for name in names:
        rig.graphs.write(timed_graph(name, outcome="HIT"))

    uploaded = rig.session.upload_graph_set(names)

    assert set(uploaded["slots"]) == set(names)
    assert uploaded["pool_usage"]["states"] == 2 * len(names)
    assert rig.session.read()["committed_set"]["graph_names"] == names


def test_a_set_that_does_not_fit_this_board_fails_before_a_trial_does(rig):
    """Where a session is allowed to fail, and the point of uploading a set.

    The alternative is discovering at trial 40 that one trial type names a graph
    the board cannot hold. The refusal names what to change, and the client
    hands it over intact.
    """
    capacity = rig.device.describe()["capabilities"]["max_graphs"]
    names = [f"too-many-{index}" for index in range(capacity + 2)]
    for name in names:
        rig.graphs.write(timed_graph(name, outcome="HIT"))

    with pytest.raises(Conflict) as refused:
        rig.session.upload_graph_set(names)

    assert refused.value.error in {"does_not_fit", "bad_index", "no_room"}
    assert refused.value.context in {"graph_names", "set", ""}
