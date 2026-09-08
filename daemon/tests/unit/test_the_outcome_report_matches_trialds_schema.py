# SPDX-License-Identifier: GPL-3.0-or-later
"""`triald_client.OutcomeReport` against the schema triald actually publishes.

The model in `triald_client.py` calls itself "a transcription, not a design",
and a transcription is only as good as the last time somebody read the
original. It was wrong: it sent `trial_id`, triald's model forbade unknown
fields and had no such field, so every trial's outcome was refused with a 422 --
and the unit test beside it asserted the field against a mock that answers 200
to anything.

This is the cheap half of the fix. triald's OpenAPI document already *is* the
machine-readable schema for this message, so no shared IDL is needed and none is
wanted (contracts INTERACTIONS.md §7): fetch it from triald's own app and check
that what this daemon sends is something that schema accepts. It fails the day
either side moves, which the transcription could not.

The expensive half is `tests/integration/test_a_whole_trial_with_triald.py`,
which posts a real outcome to a real triald. This one runs without a device.
"""

from __future__ import annotations

import pytest

from statemachined.triald_client import OutcomeReport

triald_app = pytest.importorskip(
    "triald.api.app",
    reason=(
        "triald is not installed: it is the `e2e` dependency group, which is "
        "separate because triald is a private repo and needs credentials to "
        "fetch. Run `make test-e2e`. Needs Python 3.12; this daemon runs on 3.11 "
        "too, and there these tests always skip."
    ),
)


def trialds_schema_for_the_outcome_report() -> dict:
    """The `OutcomeReport` component of triald's published OpenAPI document."""
    document = triald_app.create_app().openapi()
    return document["components"]["schemas"]["OutcomeReportModel"]


def test_every_field_this_daemon_sends_is_one_triald_knows():
    # `extra="forbid"` at the far end, so an unknown field is not ignored: it is
    # a 422 and a lost trial outcome.
    schema = trialds_schema_for_the_outcome_report()
    sent = set(OutcomeReport(trial_id=1, outcome="HIT").model_dump())
    unknown = sent - set(schema["properties"])
    assert not unknown, f"triald has no such field(s): {sorted(unknown)}"


def test_every_field_triald_requires_is_one_this_daemon_sends():
    # The other direction, and the one §5.1 was: a field triald requires and
    # this daemon omits is refused just as hard as an unknown one.
    schema = trialds_schema_for_the_outcome_report()
    sent = set(OutcomeReport(trial_id=1, outcome="HIT").model_dump())
    missing = set(schema.get("required", ())) - sent
    assert not missing, f"triald requires field(s) this daemon omits: {sorted(missing)}"


def test_the_outcome_names_this_daemon_can_send_are_names_triald_knows():
    # The outcome crosses as a name, not a code, so the two spellings of the
    # eleven .tdr outcomes have to agree exactly. They did not: code 8 is
    # `InexpectedStartSignal` in VStim's TDR.h -- a typo, and the wire contract
    # because `GetTrialOutcomeString` writes that literal into every .tdr the
    # lab has -- and this daemon had corrected it to UNEXPECTED_.
    from triald.outcomes import TrialOutcome as TrialdOutcome

    from statemachined.model.trial_outcome import TrialOutcome

    ours = {outcome.name: int(outcome) for outcome in TrialOutcome}
    theirs = {outcome.name: int(outcome) for outcome in TrialdOutcome}
    assert ours == theirs
