# SPDX-License-Identifier: GPL-3.0-or-later
"""The one call outwards, and what it deliberately leaves empty."""

from __future__ import annotations

import json

import httpx
import pytest

from statemachined.model.trial_outcome import TrialOutcome
from statemachined.model.trial_record import StateVisitRecord, TrialResultRecord
from statemachined.triald_client import OutcomeReport, TrialdClient


def a_result(exit_cause: str = "transition", duration_microseconds: int = 183_044):
    return TrialResultRecord(
        trial_id=193,
        outcome=TrialOutcome.HIT,
        visits=[
            StateVisitRecord(
                state_name="Foreperiod",
                exit_cause="timeout",
                drawn_duration_ms=500,
                entered_device_microseconds=0,
                measured_duration_microseconds=500_120,
            ),
            StateVisitRecord(
                state_name="Cue",
                exit_cause=exit_cause,
                drawn_duration_ms=1000,
                entered_device_microseconds=500_120,
                measured_duration_microseconds=duration_microseconds,
            ),
        ],
    )


def test_no_triald_is_a_configuration_and_not_a_fault():
    # A bench box has no triald, and a daemon that refused to run without one
    # would make the first thing anybody does with this package the thing that
    # fails.
    client = TrialdClient("")
    assert not client.is_configured
    assert client.report_trial_outcome(a_result()) is False


def test_the_veto_fields_are_left_at_their_defaults():
    # triald collects precise_fixation and frame_loss, and each can veto
    # acceptance on its own. This daemon has never heard of the eye monitor or
    # of vstimd; filling one in with a plausible value would make it a second
    # decision authority.
    report = OutcomeReport(trial_id=1, outcome="HIT")
    assert "precise_fixation" not in report.model_dump()
    assert "frame_loss" not in report.model_dump()
    assert report.simulated is False


def a_capturing_client(sent: dict, status_code: int = 200) -> TrialdClient:
    """A client whose far end records what it was sent and agrees to it.

    **This proves nothing about triald.** A mock answers 200 to a body triald
    would refuse, which is exactly how §5.1 of the contracts repo went unseen:
    this file asserted `trial_id` was sent while triald's schema forbade the
    field. What a mock can check is what this daemon *decides* to send. That
    the far end accepts it is the end-to-end test's job, against a real triald.
    """

    def capture(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(status_code)

    return TrialdClient(
        "http://triald.invalid", client=httpx.Client(transport=httpx.MockTransport(capture))
    )


def test_the_reaction_time_is_the_state_a_response_left():
    # Not an interpretation of what the response meant -- that is triald's --
    # only of how long the device measured the state a transition left.
    sent: dict = {}
    assert a_capturing_client(sent).report_trial_outcome(a_result()) is True

    assert sent["trial_id"] == 193
    assert sent["outcome"] == "HIT"
    assert sent["reaction_time_ms"] == 183


def test_a_trial_that_ended_on_a_timeout_has_no_reaction_time():
    # There was no response to time. Zero, rather than the duration of whatever
    # state happened to be last.
    from statemachined.triald_client import _reaction_time_milliseconds

    assert _reaction_time_milliseconds(a_result(exit_cause="timeout")) == 0


def test_a_failure_to_report_is_raised_rather_than_swallowed():
    # A trial whose outcome did not reach triald is a hole in the session's
    # record. The caller can decide to log and carry on, but it has to decide.
    client = TrialdClient("http://127.0.0.1:9")  # discard port: nothing listens
    with pytest.raises(Exception):
        client.report_trial_outcome(a_result())
