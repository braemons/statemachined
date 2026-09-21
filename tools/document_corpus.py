#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the corpus the two implementations are compared on.

**The documents are user data** (`RUST_REWRITE_PLAN.md` §4.2). A graph file
somebody wrote last year must parse identically under the Python daemon and the
Rust one, and -- just as important -- a file that is *refused* must be refused
by both. Refusals are half the contract and the half that is easy to forget.

This writes what the Python implementation says about each document. The Rust
side reads it in `daemon-rs/tests/documents.rs` and must agree.

The corpus is the real graphs in `graphs/`, plus systematic mutations of them:
one mutation per refusal rule, applied to every graph that has somewhere to
apply it. Generated rather than written out, so a rule that gains a case gains
it for every graph at once.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "daemon" / "src"))

from statemachined.model.graph_definition import GraphDefinition  # noqa: E402


def mutations(graph: dict) -> list[tuple[str, dict]]:
    """One document per refusal rule, built out of a real graph."""
    out: list[tuple[str, dict]] = []

    def variant(label: str, change) -> None:
        copied = copy.deepcopy(graph)
        try:
            change(copied)
        except (KeyError, IndexError):
            return  # this graph has nowhere to apply it
        out.append((label, copied))

    variant("unknown_field_on_the_graph", lambda g: g.update(colour="blue"))
    variant("unknown_field_on_a_state", lambda g: g["states"][0].update(colour="blue"))
    variant("entry_names_nothing", lambda g: g.update(entry="nowhere"))
    variant("no_states", lambda g: g.update(states=[]))
    variant("empty_name", lambda g: g.update(name=""))
    variant("duplicate_state_names",
            lambda g: g["states"].append(copy.deepcopy(g["states"][0])))
    variant("dangling_transition_goto",
            lambda g: g["states"][0]["transitions"][0].update(goto="nowhere"))
    variant("dangling_timeout_goto",
            lambda g: g["states"][0]["timeout"].update(goto="nowhere"))
    variant("unknown_distribution_in_timeout",
            lambda g: g["states"][0]["timeout"].update(after="no-such-distribution"))
    variant("unknown_distribution_in_hold",
            lambda g: g["states"][0]["transitions"][0].update(hold="no-such-distribution"))
    variant("bad_outcome_name",
            lambda g: next(s for s in g["states"] if s.get("outcome")).update(outcome="MARVELLOUS"))
    variant("undeclarable_outcome",
            lambda g: next(s for s in g["states"] if s.get("outcome")).update(outcome="UNDETERMINED"))
    variant("terminal_state_with_a_transition",
            lambda g: next(s for s in g["states"] if s.get("outcome")).update(
                transitions=[{"when": {"all": ["x"]}, "goto": g["entry"]}]))
    variant("terminal_state_with_on_exit",
            lambda g: next(s for s in g["states"] if s.get("outcome")).update(
                on_exit=[{"line": "valve", "kind": "high"}]))
    variant("relight_on_a_non_terminal_state",
            lambda g: next(s for s in g["states"] if not s.get("outcome")).update(
                relight_after=next(iter(g["distributions"]))))
    variant("predicate_names_nothing",
            lambda g: g["states"][0]["transitions"][0].update(when={}))
    variant("predicate_that_cannot_fire",
            lambda g: g["states"][0]["transitions"][0].update(
                when={"all": ["lever"], "none": ["lever"]}))
    variant("any_clause_entirely_forbidden",
            lambda g: g["states"][0]["transitions"][0].update(
                when={"any": ["lever"], "none": ["lever"]}))
    variant("pulse_without_width",
            lambda g: g["states"][0].update(on_entry=[{"line": "valve", "kind": "pulse"}]))
    variant("zero_width_pulse",
            lambda g: g["states"][0].update(
                on_entry=[{"line": "valve", "kind": "pulse", "pulse_ms": 0}]))
    variant("over_long_pulse",
            lambda g: g["states"][0].update(
                on_entry=[{"line": "valve", "kind": "pulse", "pulse_ms": 70000}]))
    variant("pulse_ms_on_a_high_action",
            lambda g: g["states"][0].update(
                on_entry=[{"line": "valve", "kind": "high", "pulse_ms": 5}]))
    variant("timer_action_without_a_timer",
            lambda g: g["states"][0].update(on_entry=[{"kind": "timer_start"}]))
    variant("timer_action_naming_a_line",
            lambda g: g["states"][0].update(
                on_entry=[{"kind": "timer_start", "timer": "t", "line": "valve"}]))
    variant("line_action_without_a_line",
            lambda g: g["states"][0].update(on_entry=[{"kind": "high"}]))
    variant("inverted_uniform", lambda g: g["distributions"].update(
        bad={"kind": "uniform", "minimum_ms": 500, "maximum_ms": 100}))
    variant("inverted_exponential", lambda g: g["distributions"].update(
        bad={"kind": "exponential", "minimum_ms": 500, "maximum_ms": 100, "mean_ms": 200}))
    variant("exponential_with_zero_mean", lambda g: g["distributions"].update(
        bad={"kind": "exponential", "minimum_ms": 0, "maximum_ms": 100, "mean_ms": 0}))
    variant("choice_with_no_options", lambda g: g["distributions"].update(
        bad={"kind": "choice", "options_ms": []}))
    variant("choice_with_mismatched_weights", lambda g: g["distributions"].update(
        bad={"kind": "choice", "options_ms": [10, 20], "weights": [1]}))
    variant("choice_with_all_zero_weights", lambda g: g["distributions"].update(
        bad={"kind": "choice", "options_ms": [10, 20], "weights": [0, 0]}))
    variant("choice_with_a_negative_weight", lambda g: g["distributions"].update(
        bad={"kind": "choice", "options_ms": [10, 20], "weights": [1, -1]}))
    variant("unknown_distribution_kind", lambda g: g["distributions"].update(
        bad={"kind": "poisson", "mean_ms": 100}))
    variant("negative_fixed_duration", lambda g: g["distributions"].update(
        bad={"kind": "fixed", "duration_ms": -5}))
    variant("unreachable_state", lambda g: g["states"].append(
        {"name": "orphan", "outcome": "HIT"}))
    variant("timer_loops_out_of_range",
            lambda g: g.setdefault("timers", {}).update(
                bad={"width": next(iter(g["distributions"])), "loops": 900}))
    return out


def verdict(document: dict) -> dict:
    """What the Python implementation says: accepted, or the refusal."""
    try:
        graph = GraphDefinition.model_validate(document)
    except Exception as problem:  # pydantic's ValidationError, or a ValueError
        return {"accepted": False, "refusal": str(problem)}
    return {
        "accepted": True,
        # The round trip, so the Rust side can compare what each *parsed to*
        # rather than only whether each said yes.
        "parsed": json.loads(graph.model_dump_json(exclude_none=True)),
        "warnings": graph.warnings(),
    }


def main() -> int:
    corpus = []
    for path in sorted((HERE / "graphs").glob("*.json")):
        original = json.loads(path.read_text())
        corpus.append({"name": path.stem, "document": original, **verdict(original)})
        for label, mutated in mutations(original):
            corpus.append(
                {"name": f"{path.stem}::{label}", "document": mutated, **verdict(mutated)}
            )

    out = HERE / "daemon-rs" / "tests" / "document_corpus.json"
    out.write_text(json.dumps(corpus, indent=1, sort_keys=True) + "\n")
    accepted = sum(1 for case in corpus if case["accepted"])
    print(f"{len(corpus)} documents -> {out.relative_to(HERE)}")
    print(f"  {accepted} accepted, {len(corpus) - accepted} refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
