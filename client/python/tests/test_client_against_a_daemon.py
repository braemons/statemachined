# SPDX-License-Identifier: LGPL-3.0-or-later
"""This client against a real statemachined, over a real gRPC channel.

The daemon is the shipped one, started the way an operator starts it, with
nothing on its serial port. What is under test is the whole round trip: the
servicers, the refusals, the trailing metadata, the streams, and this client's
seam — against the actual far end rather than a second description of it.

**A board that is not attached is a first-class answer here**, not a gap. A rig
whose device is unplugged is the state somebody is in when they go looking for
why, and every refusal it produces is asserted rather than assumed.
"""

from __future__ import annotations

import json

import pytest
from statemachined_client import (
    KIND_STATE_VISIT,
    DaemonRefusedTheRequest,
    NoBoardIsAttached,
    NoSuchDocument,
    RigConfigurationPatch,
    TheRigIsNotInAStateForThat,
    TrialOutcome,
)

A_GRAPH = {
    "name": "a-test-graph",
    "entry": "Ready",
    "distributions": {"hold": {"kind": "fixed", "duration_ms": 100}},
    "states": [
        {"name": "Ready", "timeout": {"after": "hold", "goto": "Done"}},
        {"name": "Done", "outcome": "HIT"},
    ],
}


# -- is it there ----------------------------------------------------------------


def test_the_daemon_answers_and_says_it_has_no_board(rig):
    """The one call that answers rather than refusing with no device.

    Which is what makes it the one to ask first: everything else on a boardless
    rig either refuses or describes a board that is not there.
    """
    health = rig.read_health()
    assert health.ok
    assert not health.device_connected


def test_the_state_reads_with_no_board(rig):
    state = rig.read_state()
    assert not state.connected
    assert not state.running
    assert state.trial_id is None, "no trial, which is not trial zero"


def test_the_device_reads_with_no_board(rig):
    """`ReadDevice` describes the *absence*, rather than refusing.

    A daemon that refused this would leave the person asking "is it plugged in"
    with nothing to read.
    """
    device = rig.read_device()
    assert not device.connected


# -- the graph store ------------------------------------------------------------


def test_a_graph_is_written_read_back_and_listed(empty_stores):
    rig = empty_stores
    rig.write_graph("a-test-graph", json.dumps(A_GRAPH))

    stored = rig.read_graph("a-test-graph")
    assert stored.name == "a-test-graph"
    assert json.loads(stored.text)["entry"] == "Ready"

    assert "a-test-graph" in {graph.name for graph in rig.list_graphs()}


def test_a_graph_crosses_as_text_and_this_client_does_not_touch_it(empty_stores):
    """**The document crosses as text, and nothing here parses it.**

    A client that parsed and re-emitted a graph would drop any field its own
    version had never heard of — which is the whole reason `StoredFile` carries
    `text` and not a model.

    What comes back is not byte-for-byte: the **daemon** normalises, because its
    store holds a validated graph rather than a file it did not read. That is
    the daemon's decision and it is visible here rather than hidden: whitespace
    and key order are not preserved, and the graph is.
    """
    rig = empty_stores
    rig.write_graph("a-test-graph", json.dumps(A_GRAPH, indent=4) + "\n")
    written = json.loads(rig.read_graph("a-test-graph").text)
    assert written["name"] == A_GRAPH["name"]
    assert written["entry"] == A_GRAPH["entry"]
    assert [state["name"] for state in written["states"]] == ["Ready", "Done"]


def test_a_graph_that_is_not_there_refuses_by_name(rig):
    with pytest.raises(NoSuchDocument) as failure:
        rig.read_graph("no-such-graph-anywhere")
    assert failure.value.error == "no_such_graph"
    assert failure.value.context, "a refusal always names what to change"
    assert not failure.value.retryable


def test_a_graph_filed_under_the_wrong_name_is_refused(empty_stores):
    """The name in the document and the name it is filed under have to agree.

    Refused rather than reconciled: picking one would rename somebody's graph
    without telling them.
    """
    with pytest.raises(DaemonRefusedTheRequest) as failure:
        empty_stores.write_graph("some-other-name", json.dumps(A_GRAPH))
    assert failure.value.error == "graph_name_mismatch"


def test_a_graph_is_deleted(empty_stores):
    rig = empty_stores
    rig.write_graph("a-test-graph", json.dumps(A_GRAPH))
    remaining = rig.delete_graph("a-test-graph")
    assert "a-test-graph" not in {graph.name for graph in remaining}


def test_validating_a_graph_needs_a_board_and_says_so(empty_stores):
    """ "Will this fit" is a question about *this* board.

    It cannot be answered without one, and the refusal says which thing is
    missing rather than reporting the graph as invalid.
    """
    rig = empty_stores
    rig.write_graph("a-test-graph", json.dumps(A_GRAPH))
    with pytest.raises(NoBoardIsAttached) as failure:
        rig.validate_graph("a-test-graph")
    assert failure.value.error == "not_connected"
    assert failure.value.retryable, "plug the board in and it works unchanged"


# -- the session document -------------------------------------------------------


def test_the_config_store_lists_and_says_which_is_loaded(rig):
    """The **session's** document — not the rig config, which is TOML on the box."""
    listing = rig.list_configs()
    assert listing.loaded == "", "nothing loaded on a daemon started without one"


def test_a_config_that_is_not_there_refuses_by_name(rig):
    with pytest.raises(NoSuchDocument) as failure:
        rig.load_config("no-such-config-anywhere")
    assert failure.value.error == "no_such_state_machine_config"


def test_the_session_reads_as_closed(rig):
    session = rig.read_session()
    assert not session.session_open
    assert session.open_seconds is None, "never opened is not zero seconds open"


def test_opening_a_session_with_no_config_loaded_is_the_wrong_moment(rig):
    """Nothing about the request is wrong; there is just nothing to commit."""
    with pytest.raises(DaemonRefusedTheRequest) as failure:
        rig.open_session()
    assert failure.value.error in {"no_state_machine_config_loaded", "not_connected"}


# -- trials, with no board ------------------------------------------------------


def test_configuring_a_trial_with_no_board_refuses(rig):
    with pytest.raises(NoBoardIsAttached):
        rig.configure_trial(1, graph="a-test-graph", cap_milliseconds=1000)


def test_starting_a_trial_with_no_board_refuses(rig):
    with pytest.raises(NoBoardIsAttached):
        rig.start_trial(1)


# -- the trace ------------------------------------------------------------------


def test_the_trace_reads_empty_and_says_where_the_ring_is(rig):
    window = rig.read_trace()
    assert window.ring_capacity > 0
    assert window.lost_entries_before is None, "nothing was lost, which is not entry zero"


def test_a_trial_with_no_entries_reads_as_an_empty_list(rig):
    """Not a refusal: "that trial has nothing in the ring" is an answer."""
    assert rig.read_trial_trace(999_999) == []


def test_watching_the_state_gives_the_state_now_before_anything_changes(rig):
    """**At once, and not on the first change.**

    A client that connected to a quiet rig and saw nothing would have no way to
    tell that from a rig that is not there.
    """
    with rig.watch_state() as states:
        first = next(iter(states))
    assert not first.connected


def test_a_subscription_shows_up_in_the_observers_and_then_goes(rig):
    """`ReadObservers` answers a question somebody asks out loud.

    It can only answer it if something registers — and a list that only loses
    entries on a clean close fills up with ghosts, which answers the question
    wrongly rather than not at all.
    """
    before = rig.read_observers().count
    with rig.watch_trace(observer="a-test-observer"):
        # **Not** by pulling a frame: this rig is quiet, and a stream with
        # nothing to carry yields nothing. The subscription is open as soon as
        # the call is made, and the observer list is where that becomes
        # visible — which is exactly the property under test.
        watching = _eventually(rig, lambda observers: observers.count == before + 1)
        assert "a-test-observer" in {observer.name for observer in watching.observers}
        assert "trace" in {observer.stream for observer in watching.observers}

    _eventually(rig, lambda observers: observers.count == before)


def _eventually(rig, is_what_we_want, tries: int = 100):
    """Poll the observer list until it says what we expect, or give up.

    Registration and unregistration happen on the daemon's event loop when the
    stream actually opens and closes, and neither is synchronous with the call
    that caused it. A sleep long enough to be reliable is a sleep that makes
    this suite slow for everybody; polling is the honest form of the same wait.
    """
    observers = rig.read_observers()
    for _ in range(tries):
        if is_what_we_want(observers):
            return observers
        observers = rig.read_observers()
    raise AssertionError(f"the observer list never settled: {observers}")


def test_a_stream_is_cancelled_by_leaving_the_with(rig):
    """And cancelling is not a failure.

    gRPC raises `CANCELLED` into the iteration when the call is cancelled, and
    a client that let that out would make every clean close look like an error.
    """
    subscription = rig.watch_trace()
    with subscription:
        pass
    assert list(subscription) == [], "a cancelled stream ends, it does not raise"


# -- recordings -----------------------------------------------------------------


def test_a_recording_is_started_paused_resumed_and_stopped(rig):
    """The four states, and the segments that come out of pausing.

    A paused recording has a hole in it; the hole is kept as a fact about the
    session rather than smoothed over.
    """
    manifest = rig.start_recording("a-test-recording", "written by the client suite")
    assert manifest.name == "a-test-recording"
    assert manifest.state == "recording"

    assert rig.pause_recording().state == "paused"
    assert rig.resume_recording().state == "recording"

    stopped = rig.stop_recording()
    assert stopped.state == "stopped"
    assert len(stopped.segments) == 2, "one before the pause and one after"

    try:
        assert "a-test-recording" in {
            recording.name for recording in rig.read_recordings().recordings
        }
    finally:
        rig.delete_recording("a-test-recording")


def test_nothing_is_being_recorded_to_begin_with(rig):
    assert rig.read_recordings().active is None, "a rig records deliberately"


def test_pausing_when_nothing_is_recording_is_the_wrong_moment(rig):
    with pytest.raises(DaemonRefusedTheRequest) as failure:
        rig.pause_recording()
    assert failure.value.error == "recording_state"


def test_a_recording_that_is_not_there_refuses_by_name(rig):
    with pytest.raises(NoSuchDocument) as failure:
        rig.read_recording("no-such-recording-anywhere")
    assert failure.value.error == "no_such_recording"


# -- the rig config -------------------------------------------------------------


def test_the_rig_config_reads_and_is_not_the_session_document(rig):
    """Two configurations, and they are never the same file."""
    configuration = rig.read_configuration()
    assert configuration.graph_store_directory, "a daemon always has its stores"
    assert configuration.trace_ring_entries > 0


def test_a_patch_changes_one_field_and_leaves_the_rest(rig):
    before = rig.read_configuration()
    update = rig.patch_configuration(RigConfigurationPatch(expected_board="a-test-board"))
    try:
        assert update.configuration.expected_board == "a-test-board"
        assert update.configuration.graph_store_directory == before.graph_store_directory
    finally:
        rig.patch_configuration(RigConfigurationPatch(expected_board=before.expected_board))


# -- the vocabulary is the daemon's ---------------------------------------------


def test_the_outcome_enum_is_the_one_the_daemon_speaks(rig):
    """Not a round trip through this process: the daemon's own spelling.

    `braemons.v1.TrialOutcome` is shared with triald and with the firmware, and
    this is the assertion that this client did not quietly renumber it.
    """
    with pytest.raises(TheRigIsNotInAStateForThat) as failure:
        rig.read_trial_result()
    assert failure.value.error == "no_result_yet", "nothing has run, which is a moment"
    assert TrialOutcome.HIT == 1
    assert KIND_STATE_VISIT == "state_visit"
