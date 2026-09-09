# SPDX-License-Identifier: GPL-3.0-or-later
"""The rig every suite in this tree pretends to be.

Two inputs, two outputs and a graph that reaches an outcome on a timeout. That
is the whole of it, and it is here rather than in each tier's own harness
because all three drive the *same* far end: a bench whose line map differed
between the daemon's tests and the client's would be two rigs, and the point of
testing both against one native device is that it is one.

Importable from `unit/`, `integration/` and `e2e/` because `tests/conftest.py`
puts this directory on `sys.path`. A module named for what it holds, never
`conftest`, so the name means the same thing from all three -- see
`tests/conftest.py`.
"""

from __future__ import annotations

#: Named lines, because a graph is authored against words and it is the daemon
#: (or `graph_set_compiler`) that turns them into bit positions. `reward_valve`
#: is safe-high on purpose: "off" is not always "low", and an active-low valve
#: driver is opened by a low.
BENCH_LINE_MAP = {
    "input_lines": [
        {"name": "start_switch", "line_index": 0},
        {"name": "lever", "line_index": 4},
    ],
    "output_lines": [
        {"name": "ready_lamp", "line_index": 0},
        {"name": "reward_valve", "line_index": 3, "safe_level_is_high": True},
    ],
}


def timed_graph(name: str, milliseconds: int = 40, *, outcome: str = "HIT") -> dict:
    """A graph that waits and then declares an outcome, with nothing to press.

    Nothing in a test process drives an input line -- a daemon sends commands
    and the device reads pins -- so a graph waiting for a lever could never be
    answered from here. That is `tests/hardware/`, with jumper wires. Reaching
    the outcome on a timeout exercises everything above the trigger, which is
    all of what the host side can be wrong about.

    `outcome` is keyword-only because `milliseconds` was the second argument
    long before it existed and thirty-odd call sites pass it positionally. It is
    an argument at all because it is the only thing about a trial worth varying:
    the device treats the code as opaque and cannot tell a HIT from a LATE, so
    what it names is the whole of what a caller can be wrong about.
    """
    return {
        "name": name,
        "entry": "Wait",
        "distributions": {"dwell": {"kind": "fixed", "duration_ms": milliseconds}},
        "states": [
            {
                "name": "Wait",
                "on_entry": [{"line": "ready_lamp", "kind": "high"}],
                "timeout": {"after": "dwell", "goto": "Done"},
            },
            {"name": "Done", "outcome": outcome},
        ],
    }
