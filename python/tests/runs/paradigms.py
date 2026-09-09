# SPDX-License-Identifier: GPL-3.0-or-later
"""The paradigms these runs drive, and what a correct board does with them.

One definition of each, because the point of this tier is that the *same* run
happens four ways -- through the daemon and straight at the device, against a
board and against the firmware built for this machine -- and a paradigm that
differed between any two of those would make the comparison meaningless.

**The board presses its own levers.** Every paradigm here that involves an input
drives it from one of the board's own outputs, through the loopback harness of
docs/operations/hardware.md: output line *n* is wired to input line *(n + 4) mod 8*, by
eight jumper wires on real hardware and by `set_native_loopback()` in software
on the host. So `cue_lamp` (output 0) *is* `lever_left` (input 4), one scan
later, and a graph that raises the lamp answers its own response window.

That is what lets these tests be the same test with a board and without one.
Before the software half existed, the chain pin -> InputConditioner ->
Transition::matches() -> a transition firing could only be exercised on silicon,
so CI never ran it at all and every host-side run ended on a timeout.

What the two halves do **not** share is timing, and nothing here asserts on it.
The host's scan is a nanosleep on a preemptible kernel; the board's is a timer
ISR. Durations are checked in `python/tests/hardware/test_timing_accuracy.py`,
which needs the board and says so.
"""

from __future__ import annotations

#: The wiring both far ends are given, named so a graph reads like a paradigm.
#:
#: The line numbers are not free: they are chosen so that each output lands on
#: the input its name implies once the harness is on. Output 0 `cue_lamp` is
#: input 4 `lever_left`, output 1 `go_signal` is input 5 `lever_right`, and so
#: on for all four pairs. Renumbering one half without the other would leave
#: every paradigm below driving a line nothing watches.
LOOPBACK_LINE_MAP = {
    "input_lines": [
        {"name": "lever_left", "line_index": 4},
        {"name": "lever_right", "line_index": 5},
        {"name": "nose_poke", "line_index": 6},
        {"name": "abort", "line_index": 7},
    ],
    "output_lines": [
        {"name": "cue_lamp", "line_index": 0},
        {"name": "go_signal", "line_index": 1},
        {"name": "reward_valve", "line_index": 2},
        {"name": "house_light", "line_index": 3},
    ],
}

#: output line name -> the input line name its jumper drives.
DRIVES = {
    "cue_lamp": "lever_left",
    "go_signal": "lever_right",
    "reward_valve": "nose_poke",
    "house_light": "abort",
}


class Paradigm:
    """A graph, and what a board that ran it correctly must say.

    Holds the assertion as data rather than as a function so that both suites
    check the same things: a helper that each suite wrote for itself would drift
    into checking the outcome in one and the whole path in the other, and the
    difference would be invisible until one of them missed a bug.
    """

    def __init__(
        self,
        name: str,
        document: dict,
        *,
        outcome: str,
        path: list[str],
        needs_loopback: bool,
        cap_milliseconds: int = 5000,
        why: str = "",
    ) -> None:
        self.name = name
        self.document = document
        self.outcome = outcome
        #: The state names, in order, that a correct run visits. Checked in
        #: full rather than only at the end: an outcome alone cannot tell a
        #: response window answered by a lever from one that timed out into a
        #: state that happens to declare the same code.
        self.path = path
        self.needs_loopback = needs_loopback
        self.cap_milliseconds = cap_milliseconds
        self.why = why

    def __repr__(self) -> str:
        return f"<paradigm {self.name}>"


def _timeout_only() -> Paradigm:
    return Paradigm(
        "timeout-only",
        {
            "name": "timeout-only",
            "entry": "Wait",
            "distributions": {"dwell": {"kind": "fixed", "duration_ms": 40}},
            "states": [
                {
                    "name": "Wait",
                    "on_entry": [{"line": "cue_lamp", "kind": "high"}],
                    "timeout": {"after": "dwell", "goto": "Done"},
                },
                {"name": "Done", "outcome": "HIT"},
            ],
        },
        outcome="HIT",
        path=["Wait", "Done"],
        needs_loopback=False,
        why="the baseline: no input at all, so it runs on a bare board too",
    )


def _self_answering() -> Paradigm:
    """The one that could never run in CI before the software harness existed."""
    return Paradigm(
        "self-answering",
        {
            "name": "self-answering",
            "entry": "Cue",
            "distributions": {"window": {"kind": "fixed", "duration_ms": 2000}},
            "states": [
                # Raising the cue lamp presses the left lever, one scan later.
                {
                    "name": "Cue",
                    "on_entry": [{"line": "cue_lamp", "kind": "high"}],
                    "timeout": {"after": "window", "goto": "Late"},
                    "transitions": [{"when": {"all": ["lever_left"]}, "goto": "Hit"}],
                },
                {"name": "Hit", "outcome": "HIT"},
                {"name": "Late", "outcome": "LATE"},
            ],
        },
        outcome="HIT",
        path=["Cue", "Hit"],
        needs_loopback=True,
        why="pin -> conditioner -> matches() -> a transition firing, end to end",
    )


def _both_levers() -> Paradigm:
    """`all` over two lines, raised in the same entry action.

    Which is the case a person cannot produce by hand: two levers released and
    pressed again inside one millisecond. The board does it to itself.
    """
    return Paradigm(
        "both-levers",
        {
            "name": "both-levers",
            "entry": "Cue",
            "distributions": {"window": {"kind": "fixed", "duration_ms": 2000}},
            "states": [
                {
                    "name": "Cue",
                    "on_entry": [
                        {"line": "cue_lamp", "kind": "high"},
                        {"line": "go_signal", "kind": "high"},
                    ],
                    "timeout": {"after": "window", "goto": "Late"},
                    "transitions": [
                        {
                            "when": {"all": ["lever_left", "lever_right"]},
                            "goto": "Hit",
                        }
                    ],
                },
                {"name": "Hit", "outcome": "HIT"},
                {"name": "Late", "outcome": "LATE"},
            ],
        },
        outcome="HIT",
        path=["Cue", "Hit"],
        needs_loopback=True,
        why="a predicate over the whole input word, not an edge on one line",
    )


def _guarded_by_none() -> Paradigm:
    """`none` blocking a predicate that would otherwise match.

    The abort line is raised by the same entry action that raises the cue, so
    the `all` half is satisfied and the `none` half is not. A board that ignored
    `none` would answer this in one scan; a correct one times out.
    """
    return Paradigm(
        "guarded-by-none",
        {
            "name": "guarded-by-none",
            "entry": "Cue",
            "distributions": {"window": {"kind": "fixed", "duration_ms": 300}},
            "states": [
                {
                    "name": "Cue",
                    "on_entry": [
                        {"line": "cue_lamp", "kind": "high"},
                        {"line": "house_light", "kind": "high"},
                    ],
                    "timeout": {"after": "window", "goto": "Late"},
                    "transitions": [
                        {
                            "when": {"all": ["lever_left"], "none": ["abort"]},
                            "goto": "Hit",
                        }
                    ],
                },
                {"name": "Hit", "outcome": "HIT"},
                {"name": "Late", "outcome": "LATE"},
            ],
        },
        outcome="LATE",
        path=["Cue", "Late"],
        needs_loopback=True,
        why="a guard that must *stop* a transition, which a passing HIT would hide",
    )


def _a_walk_through_several_states() -> Paradigm:
    """Four states in sequence, half of them answered by a lever.

    The one paradigm here with a path long enough to be worth reading: it mixes
    timeouts and predicates, so a result whose visit rows were assembled in the
    wrong order shows up as a path that is out of sequence rather than as an
    outcome that is merely wrong.
    """
    return Paradigm(
        "a-walk",
        {
            "name": "a-walk",
            "entry": "Ready",
            "distributions": {
                "short": {"kind": "fixed", "duration_ms": 30},
                "window": {"kind": "fixed", "duration_ms": 2000},
            },
            "states": [
                {
                    "name": "Ready",
                    "on_entry": [{"line": "house_light", "kind": "high"}],
                    "on_exit": [{"line": "house_light", "kind": "low"}],
                    "timeout": {"after": "short", "goto": "Foreperiod"},
                },
                {
                    "name": "Foreperiod",
                    "timeout": {"after": "short", "goto": "Cue"},
                    # Nothing raises a lever here, so this must *not* fire --
                    # the previous state lowered the house light on the way out.
                    "transitions": [{"when": {"any": ["abort"]}, "goto": "Aborted"}],
                },
                {
                    "name": "Cue",
                    "on_entry": [{"line": "go_signal", "kind": "high"}],
                    "timeout": {"after": "window", "goto": "Late"},
                    "transitions": [{"when": {"all": ["lever_right"]}, "goto": "Hit"}],
                },
                {
                    "name": "Hit",
                    "outcome": "HIT",
                    "on_entry": [{"line": "reward_valve", "kind": "pulse", "pulse_ms": 20}],
                },
                {"name": "Late", "outcome": "LATE"},
                {"name": "Aborted", "outcome": "CANCELLED"},
            ],
        },
        outcome="HIT",
        path=["Ready", "Foreperiod", "Cue", "Hit"],
        needs_loopback=True,
        why="a path worth reading, and a state that lowers what it raised",
    )


#: Every paradigm, in the order a session below runs them.
ALL = [
    _timeout_only(),
    _self_answering(),
    _both_levers(),
    _guarded_by_none(),
    _a_walk_through_several_states(),
]

#: The ones a bare board -- or a host device with no software harness -- can run.
WITHOUT_A_HARNESS = [paradigm for paradigm in ALL if not paradigm.needs_loopback]

BY_NAME = {paradigm.name: paradigm for paradigm in ALL}


def a_session_of(count: int = 12) -> list[Paradigm]:
    """`count` trials, cycling through every paradigm.

    Several runs rather than one, because the failures this tier is for are the
    ones that need a second trial to exist: a set version that goes stale, a
    result reassembled onto the previous trial's graph, a visit sequence number
    that restarts, an armed trial id that is not cleared. One trial passes all
    of those.
    """
    return [ALL[index % len(ALL)] for index in range(count)]
