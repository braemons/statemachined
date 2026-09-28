# SPDX-License-Identifier: LGPL-3.0-or-later
"""The graphs the performance suite runs, as documents a daemon commits.

Shared by the daemon half, which measures them from outside, and the profile
half, which has a daemon commit them and then measures from inside.
"""

from __future__ import annotations

from bench_rig import output


def _input_masks(count: int) -> list[list[str]]:
    """`count` distinct, non-empty sets of input lines, widest first.

    Each becomes an `all` predicate. With no output raised the loopback holds
    every input low, so none of them ever fires -- and the engine still has to
    evaluate every one on every scan, which is the cost being measured.
    """
    masks = sorted(range(1, 256), key=lambda m: (-bin(m).count("1"), m))[:count]
    return [[f"in{bit}" for bit in range(8) if mask & (1 << bit)] for mask in masks]


def a_wait_with_transitions(name: str, count: int, dwell_ms: int) -> dict:
    return {
        "name": name,
        "entry": "Wait",
        "distributions": {"dwell": {"kind": "fixed", "duration_ms": dwell_ms}},
        "states": [
            {
                "name": "Wait",
                "transitions": [
                    {"when": {"all": lines}, "goto": "Done"} for lines in _input_masks(count)
                ],
                "timeout": {"after": "dwell", "goto": "Done"},
            },
            {"name": "Done", "outcome": "HIT"},
        ],
    }


def a_ping_pong(name: str, period_ms: int) -> dict:
    """Two states handing over to each other every `period_ms`, for as long as
    the cap allows: a `visit` frame per hand-over, and a full path buffer.

    The way out to `Done` is there because the daemon refuses a graph that
    cannot end. It needs all eight inputs high, and only output 0 is ever
    raised, so the cap is what ends the trial."""
    unreachable = {"when": {"all": [f"in{line}" for line in range(8)]}, "goto": "Done"}
    return {
        "name": name,
        "entry": "Ping",
        "distributions": {"tick": {"kind": "fixed", "duration_ms": period_ms}},
        "states": [
            {
                "name": "Ping",
                "on_entry": [{"line": output(0), "kind": "high"}],
                "transitions": [unreachable],
                "timeout": {"after": "tick", "goto": "Pong"},
            },
            {
                "name": "Pong",
                "on_entry": [{"line": output(0), "kind": "low"}],
                "transitions": [unreachable],
                "timeout": {"after": "tick", "goto": "Ping"},
            },
            {"name": "Done", "outcome": "HIT"},
        ],
    }


def a_chain(name: str, states: int) -> dict:
    """`states` states in a row, each with a timeout and one transition: the
    upload grows with the graph and nothing else does."""
    chain = [
        {
            "name": f"S{index}",
            "transitions": [{"when": {"all": ["in0", "in1"]}, "goto": "Done"}],
            "timeout": {
                "after": "step",
                "goto": f"S{index + 1}" if index + 1 < states else "Done",
            },
        }
        for index in range(states)
    ]
    return {
        "name": name,
        "entry": "S0",
        "distributions": {"step": {"kind": "fixed", "duration_ms": 1}},
        "states": [*chain, {"name": "Done", "outcome": "HIT"}],
    }
