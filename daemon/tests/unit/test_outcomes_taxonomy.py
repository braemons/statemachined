# SPDX-License-Identifier: GPL-3.0-or-later
"""This daemon's copies of the `.tdr` taxonomy against the canonical one.

Three copies live in this repository -- the firmware enum, the daemon enum, and
the graph editor's menu -- and nothing but this holds them to each other or to
triald's. §5.2 of the contracts repo is what that cost: code 8 was spelled two
ways, so triald refused the outcome and `compile` refused any graph written
from triald's vocabulary, in front of a person looking at both spellings in two
web UIs.

**Outcomes cross between daemons by name**, so this is a wire check and not a
tidiness one.

The checker itself is `tools/check_outcomes.py`, which reads
`proto/braemons/v1/trial_outcome.proto` -- vendored byte-identically from
`contracts/vendored/proto/`, and `braemons.v1` because neither this daemon nor
triald owns the taxonomy. It is a script rather than a fixture so that CI can
run it before any environment exists; this suite drives the same functions so
that a drift fails a test run too.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY / "tools"))

import check_outcomes as check  # noqa: E402


def test_the_taxonomy_is_readable_at_all() -> None:
    """A regex-based reader's one failure mode is finding nothing and passing
    vacuously, so every other test here rests on this one."""
    assert len(check.taxonomy()) == 13
    assert check.taxonomy()["HIT"] == 1
    assert check.taxonomy()["UNDETERMINED"] == -1


def test_both_enums_are_the_taxonomy() -> None:
    """The firmware's and the daemon's, together: they are the two ends of the
    serial link, and a value that agrees with the table but not with each other
    would be a trial reported as the wrong outcome."""
    assert not check.problems(), "\n".join(check.problems())


def test_the_firmware_enum_is_read_through_its_own_spelling() -> None:
    """C++ says `Hit = 1` where Python says `HIT = 1`. The number is the wire
    contract; the case is local style, and the translation lives in one place."""
    assert check.firmware_enum()["HIT"] == 1
    assert check.camel_to_upper_snake("UnexpectedStartSignal") == "UNEXPECTED_START_SIGNAL"


def test_the_graph_editor_offers_exactly_the_declarable_outcomes() -> None:
    # Two are not declarable and for different reasons: UNDETERMINED is what a
    # trial holds *while* it runs, and NEVER_FINISHED is triald's verdict about
    # its own silence -- a terminal state declaring "nobody heard from me" is a
    # contradiction.
    assert check.declarable() == set(check.editor_menu())
    assert "UNDETERMINED" not in check.declarable()
    assert "NEVER_FINISHED" not in check.declarable()


def test_the_compiler_refuses_exactly_the_outcomes_the_editor_hides() -> None:
    """The editor's menu is a courtesy; this is the enforcement. A graph
    arriving from a file somebody wrote by hand never went near the menu.

    `DECLARABLE_TERMINAL_OUTCOMES` is the authority here -- the checker reads
    `NOT_DECLARABLE` out of the same module by text, and this asserts that the
    text reader and the imported module agree.
    """
    from statemachined.model.trial_outcome import DECLARABLE_TERMINAL_OUTCOMES

    assert set(DECLARABLE_TERMINAL_OUTCOMES) == check.declarable()


def test_the_checker_would_notice_a_drift() -> None:
    """Each reader, shown something it must not match."""
    assert check.python_enum_from("class Unrelated(IntEnum):\n    HIT = 1\n") == {}
    assert check.firmware_enum_from("enum class Unrelated : int8_t {\n  Hit = 1,\n};") == {}
    assert check.editor_menu_from('const OTHER = [\n  "HIT",\n];') == []
