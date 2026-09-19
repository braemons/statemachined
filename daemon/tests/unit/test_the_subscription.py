# SPDX-License-Identifier: LGPL-3.0-or-later
"""The subscription's rules, against lists of strings.

The functions take *messages* and not a socket precisely so that this file can
exist: which entry ends a trial, and what a torn stream means, are rules about a
protocol rather than about a network, and testing them through a network would
test the network.
"""

from __future__ import annotations

import json
import time

import pytest
from statemachined.client import (
    TraceStreamLost,
    TraceSubscription,
    entries,
    finished_trials,
    is_a_finished_trial,
)


def message(**fields) -> str:
    return json.dumps(fields)


VISIT = message(kind="visit", trial_id=193, state_name="Foreperiod", entry_number=1)
RESULT = message(kind="trial_result", trial_id=193, outcome="HIT", entry_number=2)
CONFIGURED = message(kind="configure", trial_id=194, entry_number=3)


# --------------------------------------------------------------- the rules ---


def test_only_a_trial_result_ends_a_trial(client):
    assert is_a_finished_trial({"kind": "trial_result"})
    # Everything else the daemon publishes -- every state entered, every setting
    # saved, every link loss -- is for the record and for whoever else is
    # watching. A consumer that acted on a `visit` would act mid-trial.
    for other in ("visit", "configure", "start", "cancel", "autorun_changed", "settings_saved"):
        assert not is_a_finished_trial({"kind": other})


def test_the_trial_ids_come_out_as_the_trials_finish():
    stream = [VISIT, RESULT, CONFIGURED, message(kind="trial_result", trial_id=194)]
    assert list(finished_trials(stream)) == [193, 194]


def test_already_decoded_entries_go_through_the_same_rule():
    """A recording replayed from `.ndjson` is the same stream.

    A rule that only worked on text would make the recovery path -- reading a
    file the daemon wrote -- a second implementation of the rule.
    """
    assert list(finished_trials([{"kind": "trial_result", "trial_id": 7}])) == [7]


def test_falling_out_of_the_ring_is_an_exception_and_not_a_shorter_answer():
    """The one thing a consumer must not be allowed to miss.

    A consumer that believed it saw everything is worse than one that knows it
    did not, which is why the daemon closes the socket rather than resuming.
    """
    lost = message(error="fell_out_of_the_ring", lost_from_entry_number=10, lost_to_entry_number=40)

    with pytest.raises(TraceStreamLost) as gone:
        list(finished_trials([VISIT, lost, RESULT]))

    assert gone.value.lost_from_entry_number == 10
    assert gone.value.lost_to_entry_number == 40
    # And it says where to go instead, because this is recoverable.
    assert "for_trial" in str(gone.value)


def test_what_arrived_before_the_loss_is_still_delivered():
    """The iteration stops at the loss; it does not throw away the prefix.

    Entries seen before a gap are as true as any others, and a consumer holding
    them can decide what to re-fetch.
    """
    lost = message(error="fell_out_of_the_ring", lost_from_entry_number=3, lost_to_entry_number=9)
    seen = []
    with pytest.raises(TraceStreamLost):
        for entry in entries([VISIT, RESULT, lost]):
            seen.append(entry["entry_number"])
    assert seen == [1, 2]


# -------------------------------------------------------- the socket around it ---


class FakeSocket:
    """A queue of frames, then silence. Whatever `recv` was told to be."""

    def __init__(self, frames, *, then=None) -> None:
        self.frames = list(frames)
        self.then = then
        self.closed = False
        self.timeouts_seen: list[float | None] = []

    def recv(self, timeout=None):
        self.timeouts_seen.append(timeout)
        if self.frames:
            return self.frames.pop(0)
        if self.then is not None:
            raise self.then
        raise TimeoutError("nothing arrived")

    def close(self) -> None:
        self.closed = True


class ConnectionClosed(Exception):
    """Named as the websockets library names it, which is how it is recognised."""


def subscription(frames, **kwargs) -> tuple[TraceSubscription, FakeSocket]:
    socket = FakeSocket(frames, then=kwargs.pop("then", None))
    return TraceSubscription("ws://rig.test/api/trace/stream", lambda: socket, **kwargs), socket


def test_leaving_the_block_is_the_whole_of_unsubscribing():
    """There is nothing to tell the daemon, and this is why it is a context manager."""
    subscribed, socket = subscription([RESULT])
    with subscribed as stream:
        assert next(stream.finished_trials()) == 193
    assert socket.closed


def test_waiting_for_one_trial_says_whether_it_ended(client):
    subscribed, _ = subscription([VISIT, RESULT])
    with subscribed as stream:
        assert stream.wait_for_trial(193, timeout_seconds=5) is True


def test_a_deadline_that_passes_is_false_and_not_an_exception():
    """"Not yet" and "never" are different, and only the caller can tell them apart.

    A rig holds nothing for a subscriber and retries nothing, so a wait that
    expires is a fact about this wait -- the trial may still be running. Raising
    would make the caller catch an exception to learn something ordinary.
    """
    subscribed, _ = subscription([])
    with subscribed as stream:
        assert stream.wait_for_trial(193, timeout_seconds=0.05) is False


def test_a_trial_that_is_not_the_one_waited_for_does_not_end_the_wait():
    subscribed, _ = subscription(
        [message(kind="trial_result", trial_id=1), message(kind="trial_result", trial_id=193)]
    )
    with subscription([])[0]:
        pass
    with subscribed as stream:
        assert stream.wait_for_trial(193, timeout_seconds=5) is True


def test_the_deadline_is_the_budget_for_the_whole_wait_and_not_for_one_receive():
    """Otherwise a wait for one thing becomes an unbounded wait for many.

    A stream that delivered a frame just inside the timeout, forever, would
    never return under a per-receive deadline -- and on a busy rig publishing a
    visit every few milliseconds, that is not a hypothetical.
    """
    subscribed, socket = subscription([VISIT] * 500, then=None)
    started = time.monotonic()
    with subscribed as stream:
        assert stream.wait_for_trial(193, timeout_seconds=0.2) is False
    assert time.monotonic() - started < 2.0
    # Each receive was told what was left of the budget, and it shrank.
    assert socket.timeouts_seen[0] > socket.timeouts_seen[-1]


def test_the_far_end_going_away_ends_the_iteration_rather_than_raising():
    """A daemon restarting is not a fault; it is a rig restarting.

    The caller's own deadline is what decides whether that mattered, so a closed
    socket ends the stream and `wait_for_trial` reports False.
    """
    subscribed, _ = subscription([VISIT], then=ConnectionClosed("going away"))
    with subscribed as stream:
        assert stream.wait_for_trial(193, timeout_seconds=5) is False


def test_a_real_fault_on_the_socket_is_not_swallowed():
    subscribed, _ = subscription([], then=OSError("the network is down"))
    with subscribed as stream, pytest.raises(OSError, match="network is down"):
        stream.wait_for_trial(193, timeout_seconds=5)


def test_using_a_subscription_that_was_never_opened_says_so():
    subscribed, _ = subscription([RESULT])
    with pytest.raises(RuntimeError, match="not open"):
        next(subscribed.finished_trials())


# ------------------------------------------------------------------ the urls ---


def test_the_stream_url_follows_the_base_urls_scheme(client):
    assert client.trace.stream_url("triald") == (
        "ws://rig.test:8081/api/trace/stream?observer=triald"
    )


def test_https_gets_wss(rig):
    import httpx
    from statemachined.client import StatemachinedClient

    secure = StatemachinedClient(
        "https://rig.test/", http_client=httpx.Client(transport=httpx.MockTransport(rig))
    )
    assert secure.trace.stream_url("triald").startswith("wss://rig.test/api/trace/stream")


def test_an_unnamed_observer_leaves_the_query_off_entirely(client):
    # A name grants nothing and there is nothing to forge, so an empty one is
    # not worth sending -- and `?observer=` would put a blank label on the
    # daemon's diagnostics page, which is worse than no label.
    assert client.trace.stream_url(None) == "ws://rig.test:8081/api/trace/stream"


def test_the_state_and_monitor_streams_have_their_own_urls(client):
    assert client.state_stream_url() == "ws://rig.test:8081/api/stream"
    assert client.device.monitor_stream_url() == "ws://rig.test:8081/api/device/monitor/stream"
