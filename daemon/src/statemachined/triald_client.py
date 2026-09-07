# SPDX-License-Identifier: LGPL-3.0-or-later
"""The one call this daemon makes outwards: what a trial did.

`POST {triald}/api/trial/outcome`, once, when a trial ends. Everything else in
this daemon is answered rather than sent, which is deliberate -- triald drives
the trial loop and a daemon that pushed unasked would be a second clock.

**What is NOT in this report is the point of the file.** triald's `PLAN.md` is
explicit that `precise_fixation` and `frame_loss` can each veto acceptance on
their own, and that triald collects them. This daemon has never heard of the eye
monitor or of vstimd; it reports what the device measured and leaves those at
their defaults. Filling one in with a plausible value would make the daemon a
second decision authority, which is the mistake dev/DAEMON.md §1 spends its
longest paragraph on.

`simulated: false`, always, and not as a formality: `triald sim` produces
outcomes with it true, and a rig whose records could not be told apart from a
simulator's is a rig whose data cannot be trusted.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict

from .model.trial_record import TrialResultRecord


class OutcomeReport(BaseModel):
    """triald's shape, as its `dev/API.md` defines it.

    A transcription, not a design: this model exists so that a change on
    triald's side is a diff here rather than a mystery at the far end.
    """

    model_config = ConfigDict(extra="forbid")

    trial_id: int
    outcome: str
    manipulandum: int = 0
    reaction_time_ms: int = 0
    terminating_interval: int = 0
    reward_ms: int = 0
    simulated: bool = False


class TrialdClient:
    """Where outcomes go, or nowhere.

    An empty base URL is a supported configuration and not a broken one: a bench
    box has no triald, and a daemon that refused to run without one would make
    the first thing anybody does with this package the thing that fails.
    """

    def __init__(self, base_url: str, timeout_seconds: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url)

    def report_trial_outcome(self, result: TrialResultRecord) -> bool:
        """Send one outcome. False when there is nowhere to send it.

        Failures raise, deliberately. A trial whose outcome did not reach triald
        is a hole in the session's record, and swallowing that here would leave
        the hole and no account of it -- the caller can decide to log and carry
        on, but it has to decide.
        """
        if not self.is_configured:
            return False
        report = OutcomeReport(
            trial_id=result.trial_id,
            outcome=result.outcome.name,
            # Everything the device measured, and nothing it did not. The
            # reaction time is the last visit's measured duration where the
            # trial ended on a transition, which is the only reading of it this
            # daemon can defend; where it ended on a timeout there was no
            # response to time and it stays zero.
            reaction_time_ms=_reaction_time_milliseconds(result),
        )
        response = httpx.post(
            f"{self.base_url}/api/trial/outcome",
            json=report.model_dump(),
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return True


def _reaction_time_milliseconds(result: TrialResultRecord) -> int:
    """How long the state that ended on a response was in.

    Zero when nothing responded. Not an interpretation of *what* the response
    meant -- that is triald's -- only of how long the device measured the state
    that a transition left.
    """
    for visit in reversed(result.visits):
        if visit.exit_cause == "transition":
            return visit.measured_duration_microseconds // 1000
    return 0
