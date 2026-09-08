# SPDX-License-Identifier: GPL-3.0-or-later
"""This daemon's copies of the `.tdr` taxonomy against the canonical one.

Three copies live in this repository -- the firmware enum, the daemon enum, and
the graph editor's menu -- and until now nothing held them to each other or to
triald's two. §5.2 of the contracts repo is what that cost: code 8 was spelled
two ways, so triald refused the outcome with a 422 and `compile` refused any
graph written from triald's vocabulary, in front of a person looking at both
spellings in two web UIs.

**Outcomes cross between daemons by name**, so this is a wire check and not a
tidiness one. `tests/contracts/` is vendored rather than imported: see its
README.
"""

from __future__ import annotations

import sys
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(CONTRACTS))

import check_outcomes as check  # noqa: E402

DAEMON = Path(__file__).resolve().parents[2]
REPOSITORY = DAEMON.parent


def taxonomy() -> dict:
    return check.load_taxonomy(CONTRACTS / "outcomes.json")


def test_both_enums_are_the_canonical_table() -> None:
    """The firmware's and the daemon's, together: they are the two ends of the
    serial link, and a value that agrees with the table but not with each other
    would be a trial reported as the wrong outcome."""
    firmware = REPOSITORY / "firmware" / "core" / "trial" / "trial.h"
    daemon = DAEMON / "src" / "statemachined" / "model" / "trial_outcome.py"
    problems = check.problems(
        taxonomy(),
        {
            "firmware/core/trial/trial.h": check.cpp_enum(firmware.read_text()),
            "model/trial_outcome.py": check.python_enum(daemon.read_text()),
        },
    )
    assert not problems, "\n".join(problems)


def test_the_graph_editor_offers_exactly_the_declarable_outcomes() -> None:
    # Two are not declarable and for different reasons: UNDETERMINED is what a
    # trial holds *while* it runs, and NEVER_FINISHED is the host's verdict about
    # its own silence -- a terminal state declaring "nobody heard from me" is a
    # contradiction.
    panel = (
        DAEMON / "src" / "statemachined" / "web" / "elements" / "graph_store_panel_element.js"
    ).read_text()
    problems = check.declarable_problems(
        taxonomy(), "graph_store_panel_element.js", check.javascript_names(panel, "OUTCOME_NAMES")
    )
    assert not problems, "\n".join(problems)


def test_the_compiler_refuses_exactly_the_outcomes_the_taxonomy_forbids() -> None:
    """The editor's menu is a courtesy; this is the enforcement. A graph arriving
    from a file somebody wrote by hand never went near the menu."""
    from statemachined.model.trial_outcome import DECLARABLE_TERMINAL_OUTCOMES

    assert set(DECLARABLE_TERMINAL_OUTCOMES) == check.declarable_names(taxonomy())


def test_the_checker_would_notice_a_drift() -> None:
    """The one failure mode of a regex-based checker: a pattern that stops
    matching finds nothing and would pass vacuously."""
    assert check.problems(taxonomy(), {"nothing": {}})
    assert check.cpp_enum("enum class Unrelated : int8_t {\n  Hit = 1,\n};") == {}
    assert check.javascript_names('const OTHER = [\n  "HIT",\n];', "OUTCOME_NAMES") == []
