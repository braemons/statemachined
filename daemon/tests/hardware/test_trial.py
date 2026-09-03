# SPDX-License-Identifier: GPL-3.0-or-later
"""A whole trial, on the board's own clock and the board's own pins.

The host suite runs the engine exhaustively and Renode runs it on emulated
peripherals, so nothing here is trying to test the state machine. What only
silicon can answer is whether a duration the device *drew* matches the duration
it *served* -- Renode's virtual time makes that question meaningless there --
and whether the result of a trial advanced in the timer ISR arrives whole while
the foreground is draining the link.
"""

from __future__ import annotations

import time

from conftest import TRIAL_OUTPUT_LINE
from harness import CancelReason, GraphUpload, Outcome, read_result
from statemachined.device.messages import ErrorCode, Field, MsgType

#: The `wait` state's fixed duration, and the tolerance it is held to.
#:
#: 2 ms is twenty scan periods at the 10 kHz target -- loose enough to survive a
#: board scanning slower than nominal, tight enough that it would catch a
#: duration served in milliseconds-since-boot rather than microseconds, or one
#: rounded through a float. Measured on an Uno R4 Minima: 500 078 us.
DWELL_MS = 500
DWELL_TOLERANCE_US = 2000


def test_a_graph_commits_and_the_device_reports_holding_it(device, two_state_graph):
    report = device.state()
    assert report["has_graph"] is True
    assert report["graph_version"] == two_state_graph.version


def test_a_graph_whose_checksum_does_not_match_is_refused(device):
    """The rolling checksum, which is what catches a *dropped* message.

    Every line carries its own CRC, so the one that went missing was perfectly
    well formed -- no per-line check can see an absence. Sending a deliberately
    wrong total is the only way to ask whether the device is really folding the
    bytes rather than accepting whatever arrives.
    """
    graph = GraphUpload(device.session, version=2)
    graph.begin(n_states=2, entry=0)
    graph.dist(0, kind="fixed", a=10)
    graph.state(0, terminal=None, timeout={"dist": 0, "target": 1})
    graph.state(1, terminal=1, timeout=None)

    try:
        graph.end(checksum="0000")
    except Exception as exc:  # DeviceError, raised by GraphUpload._send
        assert getattr(exc, "code", None) == ErrorCode.BAD_GRAPH, exc
        assert getattr(exc, "context", None) == "checksum"
    else:
        raise AssertionError("a graph with a wrong checksum was committed")

    # And the refusal left the previously committed graph alone: a failed
    # upload must not cost the device the paradigm it was already holding.
    assert device.state()["graph_version"] != 2


def test_configure_refuses_a_version_the_device_does_not_hold(device, two_state_graph):
    """A graph edit that did not land would otherwise run the old paradigm."""
    error = device.refuse(
        MsgType.CONFIGURE, trial_id=1, graph_version=two_state_graph.version + 7, cap_ms=1000
    )
    assert error.code == ErrorCode.GRAPH_MISMATCH
    assert error.context == "graph_version"


def test_start_without_configure_is_refused(device, two_state_graph):
    error = device.refuse(MsgType.START, trial_id=999)
    assert error.code == ErrorCode.NOT_READY
    assert error.context == "configure first"


def test_start_with_the_wrong_trial_id_is_refused(device, two_state_graph):
    """No trial runs that the device was not confirmed configured for."""
    armed = device.request(
        MsgType.CONFIGURE, trial_id=11, graph_version=two_state_graph.version, cap_ms=2000,
        start="serial",
    )
    assert armed[Field.MSG_TYPE] == MsgType.ARMED

    error = device.refuse(MsgType.START, trial_id=12)
    assert error.code == ErrorCode.UNKNOWN_TRIAL
    # The device is left armed; the `device` fixture disarms it.


def test_a_graph_upload_is_refused_while_a_trial_is_armed(device, two_state_graph):
    """Uploading over a graph a trial is armed against would change it underneath."""
    device.request(
        MsgType.CONFIGURE, trial_id=21, graph_version=two_state_graph.version, cap_ms=2000,
        start="serial",
    )
    error = device.refuse(MsgType.GRAPH_BEGIN, graph_version=3, n_states=2, entry=0)
    assert error.code == ErrorCode.BUSY
    assert error.context == "graph upload"


def test_a_trial_runs_and_reports_what_it_actually_did(device, two_state_graph):
    """The end-to-end path, and the only place the timing claim is tested.

    Four separate things are being asked at once, deliberately -- they need one
    trial between them and separating them would mean running four:

    1. an entry action reaches a real pin, from the timer ISR;
    2. a fixed 500 ms duration is served to within 2 ms on the board's clock;
    3. the result arrives whole, in order, while the foreground drains the link;
    4. the rolling checksum over those result lines matches.
    """
    armed = device.request(
        MsgType.CONFIGURE, trial_id=42, graph_version=two_state_graph.version, cap_ms=5000,
        start="serial",
    )
    assert armed["trial_id"] == 42

    started = device.request(MsgType.START, trial_id=42)
    assert started[Field.MSG_TYPE] == MsgType.STARTED

    # Mid-trial, while the wait state is still being served. The entry action
    # raised output line 2, and `io.out` is how anything outside the device can
    # see that a graph's line number reached a pin.
    mid = device.state()
    assert mid["running"] is True
    assert mid["io"]["out"] & (1 << TRIAL_OUTPUT_LINE), (
        f"the trial's output line is not high during the state that raises it (io.out={mid['io']['out']}); "
        "the entry action did not reach the pins"
    )

    result = read_result(device.session)
    assert result.trial_id == 42
    assert result.outcome == Outcome.HIT, "the trial did not reach its terminal state"
    assert result.checksum_matches, (
        f"result checksum {result.end.get('checksum')} does not match the "
        f"{result.computed:04X} computed over the lines that arrived: a chunk was lost"
    )

    visit = result.visit(0)
    assert visit.cause == "timeout"
    assert visit.drawn_ms == DWELL_MS, "the device served a duration it did not report drawing"
    error_us = visit.duration_us - DWELL_MS * 1000
    assert abs(error_us) <= DWELL_TOLERANCE_US, (
        f"the wait state ran {visit.duration_us} us against the {DWELL_MS} ms it was told "
        f"({error_us:+d} us). This is a finding about the board's clock or its scan."
    )


def test_the_lines_a_trial_raised_come_down_when_it_ends(device, two_state_graph):
    """A valve cannot be left open by a graph that forgot to lower it.

    Exiting a state lowers everything that state raised, through the same code
    that lowers it on any other transition -- so the line is high during `wait`
    and low once the terminal state is reached, without the graph saying so.
    """
    device.request(
        MsgType.CONFIGURE, trial_id=43, graph_version=two_state_graph.version, cap_ms=5000,
        start="serial",
    )
    device.request(MsgType.START, trial_id=43)
    read_result(device.session)

    after = device.state()
    assert after["running"] is False
    assert not (after["io"]["out"] & (1 << TRIAL_OUTPUT_LINE)), (
        f"the trial's output line is still high after the trial ended (io.out={after['io']['out']})"
    )


def test_cancel_stops_a_running_trial_and_says_why(device, two_state_graph):
    """Cancellation is a forced transition through the ordinary exit path."""
    device.request(
        MsgType.CONFIGURE, trial_id=44, graph_version=two_state_graph.version, cap_ms=5000,
        start="serial",
    )
    device.request(MsgType.START, trial_id=44)
    time.sleep(0.05)  # comfortably inside the 500 ms wait state

    ack = device.request(MsgType.CANCEL, trial_id=44)
    assert ack[Field.MSG_TYPE] == MsgType.CANCEL_ACK

    result = read_result(device.session)
    assert result.outcome == Outcome.CANCELLED
    assert result.end.get("checksum") is not None
    assert result.begin["cancel_reason"] == CancelReason.HOST
    # The state it was cancelled in still reports how long it actually ran, and
    # that it ended by cancel rather than by its timeout.
    assert result.visit(0).cause == "cancel"
    assert result.visit(0).duration_us < DWELL_MS * 1000

    assert not (device.state()["io"]["out"] & (1 << TRIAL_OUTPUT_LINE)), "a cancelled trial left a line high"


def test_cancelling_a_trial_that_is_not_running_is_refused(device):
    """Refused rather than acked, so the bridge learns nothing was cancelled."""
    error = device.refuse(MsgType.CANCEL, trial_id=12345)
    assert error.code == ErrorCode.UNKNOWN_TRIAL


def test_a_reconnect_does_not_cost_a_re_upload(device, two_state_graph):
    """`hello` resets the session but deliberately keeps the committed graph.

    A bridge that dropped its link and had to re-upload a paradigm to resume
    would turn a blip into a gap in the session -- so the greeting abandons a
    half-finished upload and any armed trial, and leaves the live graph alone.
    """
    ack = device.session.hello()
    assert ack["has_graph"] is True
    assert ack["graph_version"] == two_state_graph.version
    assert device.state()["running"] is False
