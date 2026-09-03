# SPDX-License-Identifier: LGPL-3.0-or-later
"""The eleven .tdr outcome codes, and why a terminal state carries one.

A wire contract. These values are in every .tdr the lab has written and every
analysis script that reads one, so they are NEVER renumbered -- the same
sentence firmware/core/trial/trial.h carries, for the same reason, and this is
the third copy of the table after that one and triald's.

The device treats the code as opaque: `graph_state` carries an integer, the
firmware stores it, and the result reports it back. The device cannot tell a
`HIT` from a `LATE` and does not need to, which is what keeps firmware stable
while paradigms change. Turning a name into that integer happens here.
"""

from __future__ import annotations

from enum import IntEnum


class TrialOutcome(IntEnum):
    """How a trial ended, as triald's `.tdr` records it.

    `UNDETERMINED` is the value a trial has while it is still running; it is not
    an outcome a graph may declare, and `compile` refuses a state that names it.
    """

    UNDETERMINED = -1
    NOT_STARTED = 0
    HIT = 1
    WRONG_RESPONSE = 2
    EARLY_HIT = 3
    EARLY_WRONG_RESPONSE = 4
    EARLY = 5
    LATE = 6
    EYE_ERROR = 7
    UNEXPECTED_START_SIGNAL = 8
    WRONG_START_SIGNAL = 9
    CANCELLED = 10


class TrialCancelReason(IntEnum):
    """Why a trial was cancelled, as `result_begin.cancel_reason` reports it.

    The device's own account, not something a graph declares: only `HOST` ever
    comes from outside, and the rest are things only the device can observe.
    """

    NONE = 0
    HOST = 1
    LINK_LOST = 2
    ABORT_LINE = 3
    TRIAL_TIMEOUT = 4


#: The outcomes a terminal state may declare, by the name a graph file uses.
#: Deliberately not `TrialOutcome.__members__`: UNDETERMINED is a state a trial
#: passes through rather than one it can end in, and a graph naming it would
#: produce a trial that reported "still running" for ever.
DECLARABLE_TERMINAL_OUTCOMES: dict[str, TrialOutcome] = {
    outcome.name: outcome for outcome in TrialOutcome if outcome is not TrialOutcome.UNDETERMINED
}


def terminal_outcome_for_name(outcome_name: str) -> TrialOutcome:
    """The outcome a graph's `outcome:` field names, or a ValueError saying so.

    The error lists what is legal, because the alternative -- a paradigm author
    guessing at the spelling of `EARLY_WRONG_RESPONSE` from a KeyError -- is the
    kind of thing that gets fixed by copying another graph and inheriting its
    mistakes.
    """
    try:
        return DECLARABLE_TERMINAL_OUTCOMES[outcome_name]
    except KeyError:
        legal = ", ".join(sorted(DECLARABLE_TERMINAL_OUTCOMES))
        raise ValueError(f"{outcome_name!r} is not an outcome. Legal outcomes: {legal}") from None
