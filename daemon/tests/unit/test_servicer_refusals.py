# SPDX-License-Identifier: LGPL-3.0-or-later
"""Whatever the domain raised, as the refusal a caller receives.

`refusal_for` is the one place a domain exception becomes a gRPC status, an
`error` a client switches on and a `context` naming what to change. The routes
did the same job with `raise HTTPException(404, ...)` repeated at forty call
sites, each free to pick a different number for the same thing; this is one
table, and a table is worth a test.

**Exhaustive by intent rather than by construction.** Python has no way to
enumerate every exception a package can raise, so `_RULES` is a list somebody
maintains — and the first test here is what holds that list to the exceptions
the domain actually defines. A domain exception nobody added is not a bug that
shows up as a wrong status; it shows up as `internal`, which says the daemon
broke when in fact the caller asked for a graph that is not there.
"""

from __future__ import annotations

import pytest
from statemachined.daemon.api.servicers.refusals import (
    _RULES,
    Category,
    Refusal,
    refusal_for,
)
from statemachined.daemon.event_recording import (
    BadRecordingName,
    RecordingIsInProgress,
    RecordingNameTaken,
    RecordingNotInStore,
    RecordingStateRefused,
)
from statemachined.daemon.graph_store import GraphNameMismatch, GraphNotInStore
from statemachined.graph_set_compiler import GraphNotInSet, GraphSetCompilationError
from statemachined.daemon.state_machine_config_store import (
    ConfigNameMismatch,
    ConfigNotInStore,
)
from statemachined.device.message_framing import DeviceRefusedTheCommand
from statemachined.device.statemachined_device import DeviceNotConnected, NoGraphSetCommitted

#: Every exception the domain defines for a caller's benefit. Written out here
#: so that adding one to the domain and forgetting the table is a failing test
#: rather than a wrong status in production.
EVERY_DOMAIN_REFUSAL = [
    GraphNotInStore,
    ConfigNotInStore,
    RecordingNotInStore,
    GraphNameMismatch,
    ConfigNameMismatch,
    BadRecordingName,
    RecordingNameTaken,
    RecordingStateRefused,
    DeviceNotConnected,
    NoGraphSetCommitted,
    GraphSetCompilationError,
    GraphNotInSet,
    RecordingIsInProgress,
]


@pytest.mark.parametrize("kind", EVERY_DOMAIN_REFUSAL)
def test_every_domain_exception_has_a_chosen_category(kind):
    """`internal` means *this daemon broke*, and none of these did."""
    refusal = refusal_for(kind("something went wrong"))
    assert refusal.category is not Category.THE_DAEMON_BROKE, (
        f"{kind.__name__} falls through to `internal`; add it to `_RULES`"
    )


@pytest.mark.parametrize("kind", EVERY_DOMAIN_REFUSAL)
def test_every_refusal_names_what_to_change(kind):
    """Never empty. Where there is nothing specific it repeats `error`,
    because "which field" with no answer is worse than a coarse one."""
    assert refusal_for(kind("something went wrong")).context


def test_a_set_without_the_graph_is_not_a_set_that_does_not_fit():
    """Two different things to fix, and the table is walked in order.

    `GraphNotInSet` subclasses `GraphSetCompilationError`, so a table that
    listed the general one first would answer `does_not_fit` — and send
    somebody looking for a board that was never full.
    """
    assert refusal_for(GraphNotInSet("this set has no graph called 'x'")).error == (
        "graph_not_in_set"
    )
    assert refusal_for(GraphSetCompilationError("too many states")).error == "does_not_fit"


def test_a_recording_in_progress_is_not_the_same_as_the_wrong_moment():
    """"Stop it first" and "there is nothing open" are different things to do.

    `RecordingIsInProgress` subclasses `RecordingStateRefused`, and the table
    is walked in order, so the specific one has to come first.
    """
    assert refusal_for(RecordingIsInProgress("it is being written")).error == (
        "recording_in_progress"
    )
    assert refusal_for(RecordingStateRefused("nothing is open")).error == "recording_state"


def test_the_table_only_names_exceptions_that_exist():
    for kind, _, _, _ in _RULES:
        assert isinstance(kind, type) and issubclass(kind, BaseException)


def test_a_missing_graph_reads_as_a_sentence_and_not_as_a_repr():
    """`GraphNotInStore` is a `KeyError`, and `str(KeyError(...))` adds quotes.

    A `KeyError` prints a key, and these carry a sentence written to be read by
    a person — so `statemachinectl` was printing
    `"\\"no graph called 'nope' is stored\\""`, quotes and all.
    """
    detail = refusal_for(GraphNotInStore("no graph called 'nope' is stored")).detail
    assert detail == "no graph called 'nope' is stored"
    assert not detail.startswith('"')


def test_a_missing_config_reads_as_a_sentence_too():
    detail = refusal_for(ConfigNotInStore("no config called 'nope' is stored")).detail
    assert detail == "no config called 'nope' is stored"


def test_a_missing_recording_is_not_a_keyerror_and_is_unaffected():
    """`RecordingNotInStore` is a `RuntimeError`, so nothing is stripped."""
    detail = refusal_for(RecordingNotInStore("no recording called 'nope'")).detail
    assert detail == "no recording called 'nope'"


def test_a_devices_own_refusal_arrives_with_the_devices_own_word():
    """`busy` and `graph_mismatch` are the words the board's documentation
    uses, and a caller switching on them should be switching on the board."""
    refusal = refusal_for(
        DeviceRefusedTheCommand(
            {"code": "busy", "message": "a trial is running", "context": "trial"}
        )
    )
    assert refusal.error == "busy"
    assert refusal.category is Category.WRONG_MOMENT


def test_a_refusal_raised_directly_is_passed_through_unchanged():
    made = Refusal(Category.BAD_REQUEST, "does_not_fit", "it is too big", "graphs")
    assert refusal_for(made) is made


def test_anything_else_is_the_daemon_breaking_and_says_so():
    refusal = refusal_for(ZeroDivisionError("division by zero"))
    assert refusal.category is Category.THE_DAEMON_BROKE
    assert refusal.error == "internal"


def test_an_exception_with_no_message_still_says_something():
    """A refusal whose sentence is empty is a refusal nobody can act on."""
    assert refusal_for(ZeroDivisionError()).detail == "ZeroDivisionError"
