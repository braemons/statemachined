#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the corpus the two implementations are compared on.

**The documents are user data** (`dev/RUST_PORT.md` §4.2). A graph file
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
from statemachined.model.state_machine_config import StateMachineConfig  # noqa: E402


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


def config_mutations(config: dict) -> list[tuple[str, dict]]:
    """One document per refusal rule a *config* has of its own.

    The graph rules are already covered by the graph corpus; a config reaches
    them through its own `graphs` list, so only one case is needed to show that
    it does. The rest are the config's and the line map's.
    """
    out: list[tuple[str, dict]] = []

    def variant(label: str, change) -> None:
        copied = copy.deepcopy(config)
        try:
            change(copied)
        except (KeyError, IndexError, StopIteration):
            return
        out.append((label, copied))

    variant("unknown_field_on_the_config", lambda c: c.update(colour="blue"))
    variant("unknown_field_on_the_line_map", lambda c: c["line_map"].update(colour="blue"))
    variant("unknown_field_on_an_input_line",
            lambda c: c["line_map"]["input_lines"][0].update(colour="blue"))
    variant("empty_name", lambda c: c.update(name=""))
    variant("name_that_climbs_out_of_the_directory", lambda c: c.update(name="../escape"))
    variant("name_that_hides_itself", lambda c: c.update(name=".hidden"))
    variant("name_with_a_slash", lambda c: c.update(name="a/b"))
    variant("over_long_name", lambda c: c.update(name="x" * 129))
    variant("two_graphs_of_one_name",
            lambda c: c["graphs"].append(copy.deepcopy(c["graphs"][0])))
    # A config reaches the graph rules through its own list.
    variant("a_graph_inside_it_is_refused",
            lambda c: c["graphs"][0].update(entry="nowhere"))
    variant("two_input_lines_of_one_name",
            lambda c: c["line_map"]["input_lines"].append(
                copy.deepcopy(c["line_map"]["input_lines"][0])))
    variant("two_output_lines_of_one_name",
            lambda c: c["line_map"]["output_lines"].append(
                copy.deepcopy(c["line_map"]["output_lines"][0])))
    variant("an_input_line_that_says_neither_index_nor_pin",
            lambda c: c["line_map"]["input_lines"].append({"name": "mystery"}))
    variant("an_output_line_that_says_neither_index_nor_pin",
            lambda c: c["line_map"]["output_lines"].append({"name": "mystery"}))
    variant("an_unnamed_input_line",
            lambda c: c["line_map"]["input_lines"].append({"name": "", "line_index": 31}))
    variant("two_input_lines_on_one_index",
            lambda c: c["line_map"]["input_lines"].extend(
                [{"name": "one", "line_index": 9}, {"name": "two", "line_index": 9}]))
    variant("an_input_line_past_the_word",
            lambda c: c["line_map"]["input_lines"].append({"name": "wide", "line_index": 32}))
    variant("a_negative_line_index",
            lambda c: c["line_map"]["input_lines"].append({"name": "under", "line_index": -1}))
    variant("debounce_out_of_range",
            lambda c: c["line_map"]["input_lines"][0].update(debounce_milliseconds=70000))
    variant("negative_debounce",
            lambda c: c["line_map"]["input_lines"][0].update(debounce_milliseconds=-1))
    return out


def verdict(document: dict, model) -> dict:
    """What the Python implementation says: accepted, or the refusal."""
    try:
        parsed = model.model_validate(document)
    except Exception as problem:  # pydantic's ValidationError, or a ValueError
        return {"accepted": False, "refusal": str(problem)}
    answer = {
        "accepted": True,
        # The round trip, so the Rust side can compare what each *parsed to*
        # rather than only whether each said yes.
        "parsed": json.loads(parsed.model_dump_json(exclude_none=True)),
    }
    if isinstance(parsed, GraphDefinition):
        answer["warnings"] = parsed.warnings()
    return answer


def build(directory: str, model, generate) -> list[dict]:
    corpus = []
    for path in sorted((HERE / directory).glob("*.json")):
        original = json.loads(path.read_text())
        name = path.name.removesuffix(".json")
        corpus.append({"name": name, "document": original, **verdict(original, model)})
        for label, mutated in generate(original):
            corpus.append(
                {
                    "name": f"{name}::{label}",
                    "document": mutated,
                    **verdict(mutated, model),
                }
            )
    return corpus


def main() -> int:
    out_directory = HERE / "daemon-rs" / "tests"
    for directory, model, generate, filename in (
        ("graphs", GraphDefinition, mutations, "graph_corpus.json"),
        ("configs", StateMachineConfig, config_mutations, "config_corpus.json"),
    ):
        corpus = build(directory, model, generate)
        out = out_directory / filename
        out.write_text(json.dumps(corpus, indent=1, sort_keys=True) + "\n")
        accepted = sum(1 for case in corpus if case["accepted"])
        print(f"{len(corpus):4} {directory:8} -> {out.relative_to(HERE)}")
        print(f"       {accepted} accepted, {len(corpus) - accepted} refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
