# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check a repository's copies of the .tdr outcome taxonomy against `outcomes.json`.

**Why a checker and not a generator.** `INTERACTIONS.md` §6 proposed generating
the copies. Generating them would be worse: these tables are four fifths prose,
and the prose is the part with value -- *why* code 8 is spelled correctly, why
two codes may not be declared, what `NEVER_FINISHED` means and who may assign
it. A generator deletes exactly that, or forces it into a JSON field nobody
reads in context. So the copies stay hand-written where a person will read them,
and this makes them one table.

**Vendored, not depended on.** Each repo holds a byte-identical copy of this
file and of `outcomes.json`, and calls `problems()` from its own test suite. No
build step, no package, no shared import: a repo that never syncs them keeps
working, and the day one drifts its own tests say so in its own CI.

**What it checks is names and values, not prose.** The copies are parsed with
regular expressions because they are in three languages and the alternative is
three parsers. That is fragile in exactly one direction -- a pattern that stops
matching finds *nothing*, which would pass vacuously -- so every extractor
refuses an empty result and says which file it could not read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: Names that are the whole point: a rename is a wire break, so the checker
#: refuses a file whose spelling differs even when the values line up.
Copy = tuple[str, dict[str, int]]


def load_taxonomy(outcomes_json: Path) -> dict:
    return json.loads(outcomes_json.read_text())


def canonical_values(taxonomy: dict) -> dict[str, int]:
    """Every code by its braemons name."""
    return {entry["name"]: entry["value"] for entry in taxonomy["outcomes"]}


def declarable_names(taxonomy: dict) -> set[str]:
    """The codes a graph may end on."""
    return {entry["name"] for entry in taxonomy["outcomes"] if entry["declarable"]}


def accepted_by_default(taxonomy: dict) -> dict[str, bool]:
    return {entry["name"]: entry["accepted_by_default"] for entry in taxonomy["outcomes"]}


def countable_names(taxonomy: dict) -> set[str]:
    """The codes a finished trial can carry, so a counters table needs a column.

    Everything somebody assigns. UNDETERMINED is the only one nobody does -- it
    is what a trial holds *while* it runs -- so a column for it would always
    read zero and invite the question of what it meant.
    """
    return {entry["name"] for entry in taxonomy["outcomes"] if entry["assigned_by"] != "nobody"}


# -- reading the copies ---------------------------------------------------------


def python_enum(source: str, class_name: str = "TrialOutcome") -> dict[str, int]:
    """`NAME = 7` inside one Python enum body.

    Scoped to the named class, because these files hold other enums --
    `Manipulandum`, `TrialCancelReason` -- whose values overlap this one's and
    would otherwise be read as outcomes that had drifted.
    """
    body = between(source, rf"^class {re.escape(class_name)}\(", r"^class |^def |\Z")
    return {
        name: int(value)
        for name, value in re.findall(r"^\s{4}([A-Z][A-Z_]+) = (-?\d+)\s*$", body, re.M)
    }


def cpp_enum(source: str, enum_name: str = "TrialOutcome") -> dict[str, int]:
    """`Name = 7,` inside one C++ `enum class`, in the braemons spelling.

    The firmware spells them in CamelCase, so the comparison is on the value and
    on the name *after* converting -- which is what catches a firmware enum that
    has drifted while still compiling. Scoped for the same reason as the Python
    one: `TrialCancelReason` sits directly below it.
    """
    body = between(source, rf"^enum class {re.escape(enum_name)}\b", r"^\};")
    found = re.findall(r"^\s{2}([A-Z][A-Za-z]+) = (-?\d+),", body, re.M)
    return {camel_to_upper_snake(name): int(value) for name, value in found}


def between(source: str, start_pattern: str, end_pattern: str) -> str:
    """The text after the first `start_pattern` and before the next `end_pattern`.

    Empty when the start is not found, which every caller turns into "this file
    could not be read" rather than "this file agrees".
    """
    start = re.search(start_pattern, source, re.M)
    if start is None:
        return ""
    rest = source[start.end() :]
    end = re.search(end_pattern, rest, re.M)
    return rest[: end.start()] if end else rest


def camel_to_upper_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()


def javascript_names(source: str, list_name: str) -> list[str]:
    """The outcome names in a named JS array, in order.

    Takes the array by name rather than scanning the file, so an unrelated list
    of upper-case strings cannot make this pass or fail.
    """
    match = re.search(rf"{re.escape(list_name)}\s*=\s*\[(.*?)^\];", source, re.M | re.S)
    if match is None:
        return []
    return re.findall(r'"([A-Z][A-Z_]+)"', match.group(1))


# -- the checks -----------------------------------------------------------------


def problems(taxonomy: dict, copies: dict[str, dict[str, int]]) -> list[str]:
    """Everything wrong with `copies`, as sentences. Empty means they agree.

    Args:
        copies: a label for each file, and the name-to-value table read out of
            it. A label whose table is empty is reported as unreadable rather
            than as agreeing, because a regular expression that stopped matching
            would otherwise pass silently.
    """
    canonical = canonical_values(taxonomy)
    found: list[str] = []

    for label, table in copies.items():
        if not table:
            found.append(
                f"{label}: no outcomes could be read out of it. Either the file "
                f"moved or its shape changed and this checker is now blind to it."
            )
            continue

        for name, value in sorted(table.items()):
            if name not in canonical:
                found.append(
                    f"{label}: has {name} = {value}, which is not in outcomes.json. "
                    f"Add it there first -- a code only one daemon knows is a 422 "
                    f"at the far end."
                )
            elif canonical[name] != value:
                found.append(
                    f"{label}: {name} = {value}, but outcomes.json says "
                    f"{canonical[name]}. Values are never renumbered."
                )

        for name, value in sorted(canonical.items()):
            if name not in table:
                found.append(
                    f"{label}: is missing {name} = {value}. Outcomes cross the wire "
                    f"by name, so a copy that lacks one cannot parse it."
                )

    return found


def name_set_problems(
    taxonomy: dict, label: str, offered: list[str], expected: set[str], doing: str
) -> list[str]:
    """Whether a list of names is exactly `expected`, with a reason for each miss.

    Used for the two places that hold a *subset* rather than the whole table: a
    graph editor's outcome menu, which is the declarable codes, and a counters
    table's columns, which is the countable ones.
    """
    if not offered:
        return [f"{label}: no outcome names could be read out of it."]

    by_name = {entry["name"]: entry for entry in taxonomy["outcomes"]}
    found = []
    for name in sorted(set(offered) - expected):
        entry = by_name.get(name)
        why = (
            entry.get("why_not_declarable", "it is not one of them")
            if entry
            else "it is not in outcomes.json at all"
        )
        found.append(f"{label}: offers {name}, which is not {doing}: {why}")
    for name in sorted(expected - set(offered)):
        found.append(f"{label}: is missing {name}, which is {doing}.")
    return found


def declarable_problems(taxonomy: dict, label: str, offered: list[str]) -> list[str]:
    """Whether a graph editor offers exactly the codes a graph may declare."""
    return name_set_problems(
        taxonomy, label, offered, declarable_names(taxonomy), "one a graph may declare"
    )


def countable_problems(taxonomy: dict, label: str, offered: list[str]) -> list[str]:
    """Whether a counters table has a column for every code a trial can carry."""
    return name_set_problems(
        taxonomy, label, offered, countable_names(taxonomy), "one a finished trial can carry"
    )
