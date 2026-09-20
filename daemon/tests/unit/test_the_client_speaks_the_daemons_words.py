# SPDX-License-Identifier: LGPL-3.0-or-later
"""The words `statemachined-client` publishes, against the daemon's own.

`client/python/` is a separate distribution with a separate environment, so
nothing in this suite can import it. It can *read* it, which is enough: these
are string constants, and what has to be true is that the two files say the
same word.

**Why this is worth a test.** A constant that is nearly the daemon's word is
worse than no constant at all. `KIND_STATE_VISIT = "state_visit"` compiles,
imports, type-checks and reads correctly — and every comparison against it
silently never matches, so a consumer waiting for a trial to end waits for
ever. That is precisely what happened, and it was found by a *third* repository
whose own constant disagreed rather than by anything here.

This is the same shape as `tools/check_outcomes.py`, which holds the outcome
taxonomy across the proto, the C++ enum, the Python enum and the editor menu by
reading all four as text.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from statemachined.device.state_visit_trace import KIND_STATE_VISIT, KIND_TRIAL_RESULT

#: `client/python/statemachined_client/api_types.py`, four levels up from here.
CLIENT_API_TYPES = (
    Path(__file__).resolve().parents[3]
    / "client"
    / "python"
    / "statemachined_client"
    / "api_types.py"
)


def _module_constants(path: Path) -> dict[str, object]:
    """Every module-level `NAME = <literal>`, parsed rather than imported.

    `ast` rather than `import`: the client is a distribution of its own, with
    its own dependencies, and a test that needed it installed would skip on
    every machine that had not built a second environment — which is the same
    as not existing.
    """
    tree = ast.parse(path.read_text())
    found: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = value
    return found


@pytest.fixture(scope="module")
def client_constants() -> dict[str, object]:
    if not CLIENT_API_TYPES.exists():
        pytest.skip(f"no client at {CLIENT_API_TYPES}")
    return _module_constants(CLIENT_API_TYPES)


@pytest.mark.parametrize(
    ("name", "the_daemons_word"),
    [
        ("KIND_STATE_VISIT", KIND_STATE_VISIT),
        ("KIND_TRIAL_RESULT", KIND_TRIAL_RESULT),
    ],
)
def test_the_client_spells_a_trace_kind_the_way_the_daemon_writes_it(
    client_constants, name, the_daemons_word
):
    assert client_constants.get(name) == the_daemons_word, (
        f"{name} in the client is {client_constants.get(name)!r} and this daemon "
        f"writes {the_daemons_word!r}; nothing would ever match it"
    )
