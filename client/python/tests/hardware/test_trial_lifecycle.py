# SPDX-License-Identifier: LGPL-3.0-or-later
"""A trial on the board, start to finish, and the refusals around it.

Everything here also runs on the host build, through the same daemon; what the
board adds is that an entry action reaches a real pin from the timer ISR and a
500 ms state is served by a real clock.
"""

from __future__ import annotations

import time

import pytest
from bench_rig import output, within_budget
from statemachined_client import DaemonRefusedTheRequest, TrialCancelReason, TrialOutcome

#: The output line the two-state graph raises, so a test can watch a graph's
#: line number reach a pin.
TRIAL_OUTPUT_LINE = 3

#: How long the wait state holds, and how far off the board may serve it.
DWELL_MS = 500
DWELL_TOLERANCE_US = 2000


@pytest.fixture
def two_state(rig):
    """Wait --(500 ms)--> Hit, with the trial's output line high while it waits."""
    rig.use(
        {
            "name": "two-state",
            "entry": "Wait",
            "distributions": {"wait": {"kind": "fixed", "duration_ms": DWELL_MS}},
            "states": [
                {
                    "name": "Wait",
                    "on_entry": [{"line": output(TRIAL_OUTPUT_LINE), "kind": "high"}],
                    "timeout": {"after": "wait", "goto": "Hit"},
                },
                {"name": "Hit", "outcome": "HIT"},
            ],
        }
    )
    return rig


def raised(rig) -> bool:
    return bool((rig.client.read_state().output_word or 0) & (1 << TRIAL_OUTPUT_LINE))


def test_a_committed_set_is_what_the_board_reports_holding(two_state):
    committed = two_state.client.read_device().committed_set
    assert committed is not None, "the board reports no set after the session opened"
    assert committed.set_version == two_state.set_version


def test_a_trial_runs_and_reports_what_it_actually_did(two_state):
    """The end-to-end path: an entry action reaches a pin, a fixed duration is
    served on the board's clock, and the result arrives whole."""
    client = two_state.client
    trial_id = two_state.next_trial_id()
    client.configure_trial(trial_id, graph="two-state", cap_milliseconds=5000)
    client.start_trial(trial_id)

    # Mid-trial, while the wait state is still being served: the output word is
    # how anything outside the device can see a graph's line number reach a pin.
    time.sleep(0.1)
    mid = client.read_state()
    assert mid.running
    assert (mid.output_word or 0) & (1 << TRIAL_OUTPUT_LINE), (
        f"the trial's output line is not high during the state that raises it "
        f"(output word {mid.output_word:#x}); the entry action did not reach the pins"
    )

    client.wait_for_trial(trial_id, timeout_s=10)
    result = client.read_trial_result(trial_id)
    assert result.trial_id == trial_id
    assert result.outcome == TrialOutcome.HIT, "the trial did not reach its terminal state"
    visit = result.visits[0]
    assert visit.exit_cause == "timeout"
    assert visit.drawn_duration_ms == DWELL_MS, (
        "the device served a duration it did not report drawing"
    )
    error_us = visit.measured_duration_microseconds - DWELL_MS * 1000
    within_budget(
        two_state,
        abs(error_us) <= DWELL_TOLERANCE_US,
        (
            f"the wait state ran {visit.measured_duration_microseconds} us against the "
            f"{DWELL_MS} ms it was told ({error_us:+d} us)"
        ),
    )


def test_the_lines_a_trial_raised_come_down_when_it_ends(two_state):
    """A valve cannot be left open by a graph that forgot to lower it."""
    two_state.run("two-state")
    assert not two_state.client.read_state().running
    assert not raised(two_state), "the trial's output line is still high after it ended"


def test_cancel_stops_a_running_trial_and_says_why(two_state):
    """Cancellation is a forced transition through the ordinary exit path."""
    client = two_state.client
    trial_id = two_state.next_trial_id()
    client.configure_trial(trial_id, graph="two-state", cap_milliseconds=5000)
    client.start_trial(trial_id)
    time.sleep(0.05)  # comfortably inside the 500 ms wait state

    assert client.cancel_trial(trial_id).cancelled
    client.wait_for_trial(trial_id, timeout_s=10)
    result = client.read_trial_result(trial_id)
    assert result.outcome == TrialOutcome.CANCELLED
    assert result.cancel_reason == TrialCancelReason.HOST
    # The state it was cancelled in still reports how long it actually ran.
    assert result.visits[0].exit_cause == "cancel"
    assert result.visits[0].measured_duration_microseconds < DWELL_MS * 1000
    assert not raised(two_state), "a cancelled trial left a line high"


def test_starting_a_trial_that_was_not_armed_is_refused(two_state):
    with pytest.raises(DaemonRefusedTheRequest):
        two_state.client.start_trial(987654)
    assert not two_state.client.read_state().running


def test_cancelling_a_trial_that_is_not_running_is_refused(two_state):
    """Refused rather than acknowledged, so nobody believes something stopped."""
    with pytest.raises(DaemonRefusedTheRequest):
        two_state.client.cancel_trial(12345)


def test_a_reconnect_does_not_cost_a_re_upload(two_state):
    """Reopening the link greets the board again and finds the set still there.

    A daemon that dropped its link and had to re-upload a paradigm to resume
    would turn a blip into a gap in the session.
    """
    version = two_state.client.read_device().committed_set.set_version
    reopened = two_state.client.open_link()
    assert reopened.connected
    assert reopened.committed_set is not None
    assert reopened.committed_set.set_version == version
    assert not two_state.client.read_state().running
