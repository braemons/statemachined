# SPDX-License-Identifier: LGPL-3.0-or-later
"""A session, over a real socket, against the daemon as it ships.

Deliberately few tests and each one wide. What this suite is for is the handful
of things that only a real network and a real process can be wrong about -- two
listeners on one event loop, a long upload over a real connection, a rig that
is not there -- and duplicating the integration suite's coverage here would buy
nothing but minutes per run.
"""

from __future__ import annotations

import json

import pytest
from network_harness import a_free_port, timed_graph, wait_until
from statemachined_client import (
    KIND_TRIAL_RESULT,
    DaemonIsUnavailable,
    DaemonRefusedTheRequest,
    StatemachinedClient,
    TrialOutcome,
)


def store(rig, *graphs) -> None:
    for graph in graphs:
        rig.write_graph(graph["name"], json.dumps(graph))


def test_a_rig_is_set_up_and_a_session_runs_over_a_real_socket(rig):
    """The whole arc, in the order somebody does it.

    Look at the board, store the paradigms, put them on the device, record, run
    two trials, read them back, stop. Every call in it goes over a socket to
    another process, and the subscription is a real server stream through
    `grpc.aio` -- which is the one thing no in-process suite can say anything
    about.
    """
    device = rig.read_device()
    assert device.connected is True
    assert device.capacities is not None and device.capacities.max_states > 0

    store(rig, timed_graph("go-nogo", outcome="HIT"), timed_graph("catch", outcome="EARLY"))
    uploaded = rig.upload_graph_set(["go-nogo", "catch"])
    assert uploaded.slots == {"go-nogo": 0, "catch": 1}

    rig.start_recording("over-the-network")

    outcomes = {}
    for trial_id, graph in ((1, "go-nogo"), (2, "catch")):
        rig.configure_trial(trial_id, graph=graph, cap_milliseconds=5000)
        rig.start_trial(trial_id)
        rig.wait_for_trial(trial_id, timeout_s=20)
        outcomes[trial_id] = rig.read_trial_result().outcome

    assert outcomes == {1: TrialOutcome.HIT, 2: TrialOutcome.EARLY}

    stopped = rig.stop_recording()
    assert stopped.state == "stopped"
    assert rig.read_recording("over-the-network").kind_counts["trial_result"] == 2

    # And the pull answers exactly what the stream did, over the same socket.
    for trial_id, outcome in outcomes.items():
        published = rig.read_trial_trace(trial_id)
        named = [e.payload["outcome"] for e in published if e.kind == KIND_TRIAL_RESULT]
        assert named == [outcome.name]


def test_the_subscription_is_a_real_server_stream_and_the_daemon_lists_it(rig):
    """The failure this suite exists for, in its present form.

    It used to be a WebSocket upgrade: plain `uvicorn` answers one with a 404 --
    not an error in a log, a 404 -- so the trace stream would have been dead in
    a shipped daemon while every in-process test passed, because Starlette's
    `TestClient` implements WebSockets itself. The stream is a gRPC one now, on
    the daemon's *second* listener, and the equivalent question is whether that
    listener is really there in the shipped process.
    """
    assert rig.read_observers().count == 0

    with rig.watch_trace(observer="a-real-socket", timeout_s=60):
        listed = wait_until(lambda: rig.read_observers().observers or None, timeout_seconds=10)
        assert listed, "the daemon did not see a subscriber; is the gRPC listener up?"
        assert listed[0].name == "a-real-socket"
        assert listed[0].address, "a real connection has a peer address"

    # Cancelling the call is the whole of unsubscribing, and the daemon notices
    # when the generator it was feeding is closed.
    assert wait_until(lambda: rig.read_observers().count == 0 or None, timeout_seconds=20)


def test_both_listeners_are_up_in_the_shipped_process(rig, daemon_web_port):
    """Two listeners, one loop, one command. **The shape of this daemon.**

    `grpc.aio` owns its port outright and no ASGI server speaks native gRPC, so
    `statemachined serve` binds twice: the panels on `--port` and gRPC one
    above. Nothing in-process can be wrong about this, because nothing
    in-process has a process — and a daemon that came up with only one of them
    would look perfectly healthy to whichever half you asked.
    """
    import socket

    # The client in this suite is already talking to `port + 1`, so gRPC is
    # proven by every other test here. This is the other one.
    with socket.create_connection(("127.0.0.1", daemon_web_port), timeout=5):
        pass


def test_the_state_stream_is_a_second_stream_on_the_same_listener(rig):
    """The other stream, with the opposite rule.

    Nothing here asserts *that* it coalesces -- that is the daemon's own test.
    What only this can say is that a second streaming rpc works over the same
    server, which is not implied by the first one working.
    """
    with rig.watch_state(timeout_s=10) as states:
        frame = next(iter(states), None)

    assert frame is not None
    assert frame.connected is True


def test_a_board_the_rig_can_lose_is_a_refusal_and_not_a_silence(rig):
    """The daemon answering, with a device, over a real socket.

    Which is the distinction the whole refusal hierarchy turns on: the rig is
    there, and the board may or may not be. Over a real socket, because "the
    daemon is up" is exactly what an in-process test cannot fail to be true
    about.
    """
    health = rig.read_health()
    assert health.ok is True
    assert health.device_connected is True


def test_a_rig_that_is_not_there_is_a_silence_and_not_a_refusal():
    """Nothing is listening on this port, and the message says so.

    The one failure a caller meets before anything else works -- a wrong port,
    a daemon that has not started, a rig on another subnet -- and it must not
    look like a refusal, because a refusal means the rig heard you.
    """
    absent = StatemachinedClient(f"127.0.0.1:{a_free_port()}")
    with absent, pytest.raises(DaemonIsUnavailable) as failed:
        absent.wait_until_ready(timeout_s=2)

    assert failed.value.retryable, "a daemon that is starting is worth waiting for"
    assert "127.0.0.1" in str(failed.value), "it says where it tried"


def test_pointing_at_the_browsers_port_says_which_port_gRPC_is(rig, daemon_web_port):
    """The mistake everybody makes once, and the message that ends it.

    An ASGI server answers the TCP connect and then does not speak gRPC, so the
    channel simply never becomes ready — which on its own is indistinguishable
    from a daemon that is not running.
    """
    misdirected = StatemachinedClient(f"127.0.0.1:{daemon_web_port}")
    with misdirected, pytest.raises(DaemonIsUnavailable) as failed:
        misdirected.wait_until_ready(timeout_s=3)
    assert str(daemon_web_port) in str(failed.value)


def test_a_long_upload_is_not_cut_off_by_the_ordinary_deadline(rig):
    """The set upload is the slow call, and this is where that is real.

    Over a socket, to another process, with the daemon compiling and pushing
    every graph to the device one message at a time -- which is tens of seconds
    on a UART rig and the reason nothing puts a short deadline on it.
    """
    names = [f"paradigm-{index}" for index in range(4)]
    store(rig, *(timed_graph(name, outcome="HIT") for name in names))

    uploaded = rig.upload_graph_set(names)

    assert set(uploaded.slots) == set(names)
    assert uploaded.pool_usage is not None
    assert uploaded.pool_usage.states == 2 * len(names)
    assert rig.read_session().committed_set.graph_names == names


def test_a_set_that_does_not_fit_this_board_fails_before_a_trial_does(rig):
    """Where a session is allowed to fail, and the point of uploading a set.

    The alternative is discovering at trial 40 that one trial type names a
    graph the board cannot hold. The refusal names what to change, and the
    client hands it over intact -- across a real socket, in the trailing
    metadata of a call that was aborted.
    """
    capacity = rig.read_device().capacities.max_graphs
    names = [f"too-many-{index}" for index in range(capacity + 2)]
    store(rig, *(timed_graph(name, outcome="HIT") for name in names))

    with pytest.raises(DaemonRefusedTheRequest) as refused:
        rig.upload_graph_set(names)

    assert refused.value.error in {"does_not_fit", "bad_index", "no_room"}
    assert refused.value.detail, "a refusal that crossed a socket kept its sentence"
    assert refused.value.context, "and the field to change"
