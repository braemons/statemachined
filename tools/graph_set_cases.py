#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Python graph-set compiler says, case by case.

**The upload is where a wrong index stops being visible.** A mask one bit out,
a target one state out, a distribution that landed at pool entry 2 instead of
3: each is a valid message, uploads cleanly, and runs a different experiment.
The device validates shape, not intent. So the Rust compiler is held to the
Python one message by message -- every field of every body, in order -- and to
the tables a result is read back through.

Refusals are compared for *whether*, and the wording is recorded so a failure
can show both sentences side by side.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "daemon" / "src"))

# The device package first: it imports the compiler, and the compiler imports
# it, and only this order resolves.
import statemachined.device  # noqa: E402, F401
from statemachined.graph_set_compiler import (  # noqa: E402
    DeviceCapabilities,
    GraphSetCompilationError,
    compile_graph_set_for_device,
)
from statemachined.model.graph_definition import GraphDefinition  # noqa: E402
from statemachined.model.line_map import LineMap  # noqa: E402

#: The Uno R4 Minima's greeting, as the device sends it: pools under `caps`,
#: line counts beside them.
REFERENCE_GREETING = {
    "caps": {
        "max_line": 512,
        "max_states": 32,
        "max_transitions": 64,
        "max_output_actions": 64,
        "max_distributions": 32,
        "max_choice_options": 32,
        "max_path": 255,
        "max_graphs": 20,
        "max_timers": 8,
        "first_timer_line": 24,
    },
    "n_input_lines": 8,
    "n_output_lines": 8,
}

#: Every name the three shipped graphs use, at indices that are not their order,
#: so a lookup by position rather than by name comes out wrong.
RIG_LINE_MAP = {
    "input_lines": [
        {"name": "start_switch", "line_index": 0},
        {"name": "abort", "line_index": 1},
        {"name": "lever_left", "line_index": 4},
        {"name": "lever_right", "line_index": 5},
        {"name": "lever", "line_index": 6},
    ],
    "output_lines": [
        {"name": "ready_lamp", "line_index": 0},
        {"name": "cue_lamp", "line_index": 1},
        {"name": "error_lamp", "line_index": 2},
        {"name": "reward_valve", "line_index": 3},
    ],
}


def greeting(**caps) -> dict:
    changed = copy.deepcopy(REFERENCE_GREETING)
    changed["caps"].update(caps)
    return changed


def shipped(name: str) -> dict:
    return json.loads((HERE / "graphs" / f"{name}.json").read_text())


def small(name: str, foreperiod: dict | None = None, **overrides) -> dict:
    """Wait --(foreperiod)--> Cue --(a lever)--> Hit, with a lamp and a reward."""
    graph = {
        "name": name,
        "entry": "Wait",
        "distributions": {
            "foreperiod": foreperiod or {"kind": "fixed", "duration_ms": 500},
        },
        "states": [
            {
                "name": "Wait",
                "on_entry": [{"line": "ready_lamp", "kind": "high"}],
                "timeout": {"after": "foreperiod", "goto": "Cue"},
                "transitions": [{"when": {"all": ["abort"]}, "goto": "Aborted"}],
            },
            {
                "name": "Cue",
                "transitions": [
                    {"when": {"all": ["lever_left"], "none": ["abort"]}, "goto": "Hit"}
                ],
                "timeout": {"after": "foreperiod", "goto": "Aborted"},
            },
            {
                "name": "Hit",
                "outcome": "HIT",
                "on_entry": [{"line": "reward_valve", "kind": "pulse", "pulse_ms": 40}],
            },
            {"name": "Aborted", "outcome": "CANCELLED"},
        ],
    }
    graph.update(overrides)
    return graph


def with_a_timer(name: str = "with-a-timer", timer_name: str = "reward", **timer) -> dict:
    """One state, one timer, and enough distributions to name."""
    spec = {"width": "open", "delay": "foreperiod", "line": "reward_valve"}
    spec.update(timer)
    return {
        "name": name,
        "entry": "done",
        "distributions": {
            "foreperiod": {"kind": "fixed", "duration_ms": 200},
            "open": {"kind": "fixed", "duration_ms": 40},
            "gap": {"kind": "uniform", "minimum_ms": 10, "maximum_ms": 30},
        },
        "timers": {timer_name: spec},
        "states": [
            {
                "name": "done",
                "outcome": "HIT",
                "on_entry": [{"kind": "timer_start", "timer": timer_name}],
            }
        ],
    }


def chain(name: str, state_count: int, transitions_per_state: int = 1) -> dict:
    """`state_count` states in a line, to fill a pool to a chosen number."""
    states = []
    for index in range(state_count - 1):
        states.append(
            {
                "name": f"s{index}",
                "transitions": [
                    {"when": {"all": ["lever"]}, "goto": f"s{index + 1}"}
                    for _ in range(transitions_per_state)
                ],
            }
        )
    states.append({"name": f"s{state_count - 1}", "outcome": "HIT"})
    return {"name": name, "entry": "s0", "states": states}


def cases() -> list[dict]:
    def case(why, graphs, line_map=RIG_LINE_MAP, hello_ack=REFERENCE_GREETING, set_version=7):
        return {
            "why": why,
            "graphs": graphs,
            "line_map": line_map,
            "hello_ack": hello_ack,
            "set_version": set_version,
        }

    every_distribution = small(
        "every-distribution",
        distributions={
            "foreperiod": {"kind": "fixed", "duration_ms": 500},
            "flat": {"kind": "uniform", "minimum_ms": 300, "maximum_ms": 700},
            "hazard": {"kind": "exponential", "minimum_ms": 500, "maximum_ms": 2500, "mean_ms": 900},
            "listed": {"kind": "choice", "options_ms": [100, 200, 400]},
            "weighted": {"kind": "choice", "options_ms": [100, 200], "weights": [3, 1]},
        },
    )

    held = small(
        "held",
        states=[
            {
                "name": "Wait",
                "timeout": {"after": "foreperiod", "goto": "Hit"},
                "transitions": [
                    {
                        "when": {"all": ["lever_left"], "any": ["lever", "lever_right"]},
                        "goto": "Hit",
                        "hold": "foreperiod",
                        "fire_if_already_true_on_entry": True,
                    }
                ],
            },
            {"name": "Hit", "outcome": "HIT"},
        ],
    )

    relit = small("relit")
    relit["distributions"]["iti"] = {"kind": "uniform", "minimum_ms": 1000, "maximum_ms": 2000}
    relit["states"][2]["relight_after"] = "iti"

    exits = small("exits")
    exits["states"][0]["on_exit"] = [
        {"line": "ready_lamp", "kind": "low"},
        {"line": "cue_lamp", "kind": "toggle"},
    ]
    exits["states"][1]["on_entry"] = [{"line": "cue_lamp", "kind": "high"}]
    exits["states"][1]["on_exit"] = [{"line": "cue_lamp", "kind": "low"}]

    waits_on_a_timer = {
        "name": "waits-on-a-timer",
        "entry": "wait",
        "distributions": {"open": {"kind": "fixed", "duration_ms": 40}},
        "timers": {"reward": {"width": "open"}, "second": {"width": "open"}},
        "states": [
            {
                "name": "wait",
                "on_entry": [{"kind": "timer_start", "timer": "second"}],
                "on_exit": [{"kind": "timer_cancel", "timer": "reward"}],
                "transitions": [
                    {"when": {"none": ["reward"], "all": ["lever_left", "second"]}, "goto": "done"}
                ],
            },
            {"name": "done", "outcome": "HIT"},
        ],
    }

    unknown_input = small("unknown-input")
    unknown_input["states"][1]["transitions"][0]["when"]["all"] = ["nose_poke"]
    unknown_output = small("unknown-output")
    unknown_output["states"][2]["on_entry"][0]["line"] = "water"
    names_no_timer = small("names-no-timer")
    names_no_timer["states"][2]["on_entry"] = [{"kind": "timer_start", "timer": "nobody"}]

    choices = small(
        "choices",
        distributions={
            "foreperiod": {"kind": "choice", "options_ms": list(range(100, 2100, 100))},
        },
    )

    return [
        # -- what the rigs actually run
        case("go-nogo, as shipped", [shipped("go-nogo")]),
        case("state-walk, as shipped", [shipped("state-walk")]),
        case("two-alternative-forced-choice, as shipped",
             [shipped("two-alternative-forced-choice")]),
        case("all three shipped graphs in one set, sharing a pool",
             [shipped("go-nogo"), shipped("state-walk"), shipped("two-alternative-forced-choice")]),
        case("the same three in the other order: slots follow the list",
             [shipped("two-alternative-forced-choice"), shipped("state-walk"), shipped("go-nogo")]),
        # -- the body of each message
        case("the small graph: every message kind but timers", [small("small")]),
        case("every kind of distribution, weighted and not", [every_distribution]),
        case("a hold, a level transition, and all three predicate terms", [held]),
        case("a terminal state's relight names a pool entry", [relit]),
        case("exit actions follow entry actions, and low and toggle carry no width", [exits]),
        case("two graphs drawing the same duration share one entry",
             [small("a"), small("b")]),
        case("durations that differ in one parameter are separate entries",
             [small("a"), small("b", {"kind": "fixed", "duration_ms": 501})]),
        case("a choice's options are counted into their own pool", [choices]),
        # -- global timers
        case("a timer with its defaults", [with_a_timer()]),
        case("a timer with every field set",
             [with_a_timer(gap="gap", loops=3, active_low=True, trial_bound=True,
                           when={"all": ["lever"], "any": ["abort", "lever_left"],
                                 "none": ["start_switch"]})]),
        case("a timer with no line, looping for ever",
             [with_a_timer(line="", delay=None, loops=0)]),
        case("a predicate naming a timer is a bit above the lines", [waits_on_a_timer]),
        case("a board whose timers start lower down the word",
             [waits_on_a_timer], hello_ack=greeting(first_timer_line=16)),
        case("timers are numbered across the set, not per graph",
             [with_a_timer("first", "reward"), waits_on_a_timer.copy() | {"name": "second",
              "timers": {"later": {"width": "open"}}, "states": [
                  {"name": "wait", "on_entry": [{"kind": "timer_start", "timer": "reward"}],
                   "transitions": [{"when": {"none": ["later", "reward"]}, "goto": "done"}]},
                  {"name": "done", "outcome": "HIT"}]}]),
        # -- refusals before anything is counted
        case("an empty set", []),
        case("more graphs than the board has slots",
             [small("a"), small("b"), small("c")], hello_ack=greeting(max_graphs=2)),
        case("the same graph named twice", [small("a"), small("b"), small("a")]),
        case("a global timer declared by two graphs",
             [with_a_timer("first"), with_a_timer("second")]),
        case("more timers than the board holds", [with_a_timer()],
             hello_ack=greeting(max_timers=0)),
        case("a timer named like an input line", [with_a_timer(timer_name="abort")]),
        # -- refusals that name a place
        case("an input line this rig has not got", [unknown_input]),
        case("an output line this rig has not got", [unknown_output]),
        case("a timer driving an output this rig has not got",
             [with_a_timer(line="water")]),
        case("a timer gated on an input this rig has not got",
             [with_a_timer(when={"all": ["nose_poke"]})]),
        case("an action starting a timer nobody declared", [names_no_timer]),
        case("a line configured by pin and never resolved is not a missing line",
             [small("small")],
             line_map={**RIG_LINE_MAP, "input_lines": [
                 {**line, "line_index": None, "pin_label": "D7"} if line["name"] == "abort"
                 else line for line in RIG_LINE_MAP["input_lines"]]}),
        # -- refusals of a set that does not fit
        case("too many states", [chain("long", 33)]),
        case("exactly as many states as the board holds", [chain("long", 32)]),
        case("too many transitions", [chain("busy", 20, transitions_per_state=4)]),
        case("too many output actions", [small("a"), small("b")],
             hello_ack=greeting(max_output_actions=3)),
        case("too many distributions",
             [small("a"), small("b", {"kind": "fixed", "duration_ms": 501})],
             hello_ack=greeting(max_distributions=1)),
        case("too many choice options", [choices], hello_ack=greeting(max_choice_options=19)),
        case("several pools overflowing name every one", [chain("long", 40)],
             hello_ack=greeting(max_transitions=10)),
        case("graphs that fit alone and not together",
             [chain("one", 20), chain("two", 20)]),
    ]


def verdict(case: dict) -> dict:
    graphs = [GraphDefinition.model_validate(graph) for graph in case["graphs"]]
    line_map = LineMap.model_validate(case["line_map"])
    capabilities = DeviceCapabilities.from_hello_ack(case["hello_ack"])
    try:
        compiled = compile_graph_set_for_device(
            graphs, line_map, capabilities, set_version=case["set_version"]
        )
    except GraphSetCompilationError as problem:
        return {"compiled": None, "refused": str(problem)}
    return {
        "compiled": {
            "set_version": compiled.set_version,
            "upload_messages": [
                {"msg_type": str(message.msg_type), "fields": message.fields}
                for message in compiled.upload_messages
            ],
            "graphs_by_slot": [
                {
                    "name": graph.name,
                    "slot": graph.slot,
                    "state_names_by_index": graph.state_names_by_index,
                    "transition_target_names_by_state_index":
                        graph.transition_target_names_by_state_index,
                    "distribution_pool_index_by_name": graph.distribution_pool_index_by_name,
                }
                for graph in compiled.graphs_by_slot
            ],
            "pool_usage": compiled.pool_usage,
            "pool_capacity": compiled.pool_capacity,
        },
        "refused": None,
    }


def main() -> int:
    out = HERE / "daemon-rs" / "tests" / "graph_set_cases.json"
    corpus = [{**case, "answer": verdict(case)} for case in cases()]
    out.write_text(json.dumps(corpus, indent=1) + "\n")
    refused = sum(1 for case in corpus if case["answer"]["refused"])
    messages = sum(
        len(case["answer"]["compiled"]["upload_messages"])
        for case in corpus
        if case["answer"]["compiled"]
    )
    print(f"{len(corpus)} graph-set cases -> {out.relative_to(HERE)}")
    print(f"  {len(corpus) - refused} compiled ({messages} messages), {refused} refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
