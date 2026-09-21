#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Every copy of the `.tdr` taxonomy in this repository, against the enum.

`proto/braemons/v1/trial_outcome.proto` **is** the taxonomy, and it is vendored
here byte-identically from `contracts/vendored/proto/`. It is `braemons.v1`
rather than either daemon's package because neither owns it: this device
reports an outcome and triald records one, and a package named after one of the
two would make the other import its neighbour's interface to say what a trial
did.

This holds the three places in this repository that restate it:

  * `firmware/core/trial/trial.h` — the C++ enum the board stores and reports;
  * `statemachined.model.trial_outcome.TrialOutcome` — the Python enum the
    daemon works in;
  * `OUTCOME_NAMES` in the graph editor — the menu a person picks a terminal
    state's outcome from.

**Outcomes cross between daemons by name**, because protobuf's JSON mapping
puts an enum value's name on the wire, so a copy that drifts is not cosmetic.
`contracts/INTERACTIONS.md` §5.2 is what it cost: code 8 was spelled two ways,
so one outcome was unparseable at the far end and any graph written from the
other vocabulary was refused at compile — by a person with both spellings in
front of them in two web UIs.

This replaces `tests/contracts/check_outcomes.py` + `outcomes.json`, vendored
from the contracts repository. The taxonomy is a protobuf enum now, which is
the thing that JSON file was imitating.

**One fact the JSON carried is not here: which outcomes a graph may declare.**
That is this daemon's own rule and nobody else's — `UNDETERMINED` is what a
trial holds *while* it runs, and `NEVER_FINISHED` is triald's verdict about its
own silence, so a terminal state declaring either is a contradiction. It lives
where it is enforced, in `NOT_DECLARABLE`, and this checker holds the editor's
menu to *that* rather than to a third copy in a shared file.

Everything is text-matched rather than imported, so this runs with nothing
installed but `python3` — including in CI before any environment exists.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PROTO = HERE / "proto" / "braemons" / "v1" / "trial_outcome.proto"
FIRMWARE_ENUM = HERE / "firmware" / "core" / "trial" / "trial.h"
PYTHON_ENUM = HERE / "daemon" / "src" / "statemachined" / "model" / "trial_outcome.py"
EDITOR = HERE / "client" / "web" / "elements" / "graph_store_panel_element.js"


def taxonomy() -> dict[str, int]:
    """The enum, by name."""
    body = between(PROTO.read_text(), r"enum TrialOutcome\s*\{", r"^\}")
    return {
        name: int(value)
        for name, value in re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(-?\d+)\s*;", body, re.M)
    }


# Each reader takes source text and has a wrapper that reads its file. The
# split is not ceremony: a regex reader's one failure mode is a pattern that
# stops matching, which finds nothing and passes vacuously, and the only way to
# test for that is to hand it something it must not match.


def python_enum_from(source: str) -> dict[str, int]:
    body = between(source, r"class TrialOutcome\(.*?\):", r"^class |\Z")
    return {
        name: int(value)
        for name, value in re.findall(r"^\s{4}([A-Z][A-Z0-9_]*)\s*=\s*(-?\d+)", body, re.M)
    }


def firmware_enum_from(source: str) -> dict[str, int]:
    """The C++ enum, whose values are `Hit = 1` rather than `HIT = 1`.

    The *number* is the wire contract between the board and this daemon; the
    C++ spelling is local style. Upper-snaking it is what lets one table check
    both, and `camel_to_upper_snake` is the only place that translation lives.
    """
    body = between(source, r"enum class TrialOutcome[^{]*\{", r"^\};")
    return {
        camel_to_upper_snake(name): int(value)
        for name, value in re.findall(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*=\s*(-?\d+)", body, re.M)
    }


def editor_menu_from(source: str) -> list[str]:
    body = between(source, r"const OUTCOME_NAMES\s*=\s*\[", r"^\];")
    return re.findall(r'"([A-Z][A-Z0-9_]*)"', body)


def python_enum() -> dict[str, int]:
    return python_enum_from(PYTHON_ENUM.read_text())


def firmware_enum() -> dict[str, int]:
    return firmware_enum_from(FIRMWARE_ENUM.read_text())


def editor_menu() -> list[str]:
    return editor_menu_from(EDITOR.read_text())


def declarable() -> set[str]:
    """What `NOT_DECLARABLE` leaves, read out of the module that enforces it."""
    body = between(PYTHON_ENUM.read_text(), r"NOT_DECLARABLE\s*=\s*frozenset\(\{", r"\}\)")
    forbidden = set(re.findall(r"TrialOutcome\.([A-Z][A-Z0-9_]*)", body))
    return set(taxonomy()) - forbidden


def camel_to_upper_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()


def between(source: str, start: str, end: str) -> str:
    """The text between two patterns, or nothing at all.

    Returning empty rather than raising when the opening pattern is missing is
    what lets `problems()` say "this reader can see nothing" in its own words —
    and what lets a test hand a reader source it must not match.
    """
    match = re.search(start, source, re.M | re.S)
    if match is None:
        return ""
    rest = source[match.end() :]
    stop = re.search(end, rest, re.M)
    return rest[: stop.start()] if stop else rest


def problems() -> list[str]:
    canonical = taxonomy()
    if not canonical:
        return ["proto/braemons/v1/trial_outcome.proto has no enum values this reader can see"]

    found: list[str] = []
    for label, copy in (
        ("statemachined.model.trial_outcome.TrialOutcome", python_enum()),
        ("firmware/core/trial/trial.h", firmware_enum()),
    ):
        if not copy:
            found.append(f"{label} has no values this reader can see — the checker needs updating")
            continue
        for name, value in sorted(copy.items()):
            if name not in canonical:
                found.append(f"{label} has {name}, which the taxonomy does not")
            elif canonical[name] != value:
                found.append(
                    f"{label} spells {name} = {value}; the taxonomy says {canonical[name]}"
                )
        for name in sorted(set(canonical) - set(copy)):
            found.append(f"{label} is missing {name}")

    offered = editor_menu()
    if not offered:
        found.append("the graph editor's OUTCOME_NAMES has no names this reader can see")
    may_declare = declarable()
    for name in sorted(may_declare - set(offered)):
        found.append(f"the graph editor does not offer {name}, which a graph may declare")
    for name in sorted(set(offered) - may_declare):
        reason = " (NOT_DECLARABLE)" if name in canonical else " (not an outcome at all)"
        found.append(f"the graph editor offers {name}{reason}")

    return found


def main() -> int:
    found = problems()
    if found:
        print("the taxonomy and its copies disagree:")
        for problem in found:
            print(f"  {problem}")
        print()
        print("proto/braemons/v1/trial_outcome.proto is the taxonomy, and it is vendored")
        print("from contracts/. Change it there first, then follow it in the firmware enum,")
        print("the Python enum and the graph editor's menu.")
        return 1
    print(f"the taxonomy and its three copies agree ({len(taxonomy())} outcomes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
