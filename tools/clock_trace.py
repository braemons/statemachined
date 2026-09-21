#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the Python clock correlation over a swept input space, and record it.

**Why generated and not recorded.** `dev/RUST_PORT.md` §4.1 said to check
this against recordings the Python daemon made. There are no recent ones, and
on reflection generated traces are the better instrument anyway: a recording
covers whatever happened to occur on some Tuesday, while a sweep covers the
cases this arithmetic actually turns on -- the wrap boundary, a backwards jump
just under the threshold, several wraps in a row, a ping that is worse than the
one before it. A recording would exercise the straight line through the middle
and none of the edges.

The trace is a list of operations and what Python answered. `daemon-rs/tests/clock.rs`
replays the operations and must give the same answers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "daemon" / "src"))

from statemachined.device.device_clock_correlation import (  # noqa: E402
    DEVICE_CLOCK_WRAP_MICROSECONDS,
    WRAP_DETECTION_THRESHOLD_MICROSECONDS,
    DeviceClockCorrelation,
)

WRAP = DEVICE_CLOCK_WRAP_MICROSECONDS
THRESHOLD = WRAP_DETECTION_THRESHOLD_MICROSECONDS


def programme() -> list[dict]:
    """Every operation, in order. One list so the state carries between them."""
    steps: list[dict] = []

    def unwrap(raw: int, why: str) -> None:
        steps.append({"op": "unwrap", "raw": raw, "why": why})

    def ping(before: float, raw: int, after: float, why: str) -> None:
        steps.append({"op": "ping", "before": before, "raw": raw, "after": after, "why": why})

    def query(unwrapped: int, why: str) -> None:
        steps.append({"op": "query", "unwrapped": unwrapped, "why": why})

    def forget(why: str) -> None:
        steps.append({"op": "forget", "why": why})

    # -- before any ping: an estimate must not be invented -------------------
    query(0, "no ping yet, so there is no estimate to give")
    unwrap(0, "the first reading of a freshly powered board")
    unwrap(1_000, "ordinary forward motion")

    # -- the wrap, from both sides -------------------------------------------
    unwrap(WRAP - 1, "one microsecond before the counter wraps")
    unwrap(0, "the wrap itself")
    unwrap(1_000, "just after it")
    unwrap(WRAP - 1, "and round again")
    unwrap(5, "a second wrap")

    # -- a backwards jump that is NOT a wrap ---------------------------------
    unwrap(WRAP // 4, "forward, to sit well inside the range")
    unwrap(WRAP // 4 - 1_000, "backwards by 1 ms: out-of-order, not a wrap")
    unwrap(WRAP // 4 + 1_000, "and on again")

    # -- exactly at the threshold, which is the boundary of the rule ---------
    unwrap(THRESHOLD, "sit at the threshold")
    unwrap(0, "backwards by exactly the threshold: NOT a wrap, the test is >")
    unwrap(THRESHOLD + 1, "sit one past it")
    unwrap(0, "backwards by one more than the threshold: a wrap")

    # -- pings: the best is kept, not the newest -----------------------------
    ping(1_700_000_000.000_000, 10_000, 1_700_000_000.002_000, "first, 2 ms round trip")
    query(10_000, "at the observation itself")
    query(10_000 + 1_000_000, "one device second later")
    query(10_000 - 1_000_000, "one device second earlier")
    ping(1_700_000_010.000_000, 10_000_000, 1_700_000_010.050_000, "worse, 50 ms: ignored")
    query(10_000, "so the answer must not have moved")
    ping(1_700_000_020.000_000, 20_000_000, 1_700_000_020.000_500, "better, 0.5 ms: kept")
    query(20_000_000, "and now it has")

    # -- rounding of the round trip, which biases the bound if truncated -----
    ping(1_700_000_030.000_000, 30_000_000, 1_700_000_030.000_000_4, "0.4 us rounds to 0")
    query(30_000_000, "an uncertainty of zero is legal and must be said")
    ping(1_700_000_040.000_000, 40_000_000, 1_700_000_040.000_001_6, "1.6 us rounds to 2")

    # -- odd round trips, where the bound floors -----------------------------
    forget("start clean for the halving")
    ping(1_700_000_050.000_000, 50_000_000, 1_700_000_050.000_007, "7 us: the bound floors to 3")
    query(50_000_000, "half of seven, floored")

    # -- a reconnect forgets everything --------------------------------------
    forget("a reset device restarts its clock from zero")
    query(0, "so there is no estimate again")
    unwrap(0, "and the unwrapping starts over")
    unwrap(WRAP - 1, "a backwards jump across a forget is not a wrap of the old timeline")

    # -- the iso8601 rendering, including the fraction that rounds up --------
    forget("clean, for the formatting")
    ping(1_700_000_000.000_000, 0, 1_700_000_000.000_002, "a tight ping to anchor it")
    for microseconds, why in (
        (0, "on the second"),
        (1, "one microsecond past it"),
        (500_000, "half a second"),
        (999_999, "the last whole microsecond of the second"),
    ):
        query(microseconds, f"iso8601: {why}")

    return steps


def run(steps: list[dict]) -> list[dict]:
    clock = DeviceClockCorrelation()
    out = []
    for step in steps:
        answer: dict = {}
        if step["op"] == "unwrap":
            answer["unwrapped"] = clock.unwrap_device_microseconds(step["raw"])
        elif step["op"] == "ping":
            try:
                clock.observe_ping_round_trip(step["before"], step["raw"], step["after"])
                answer["refused"] = False
            except ValueError as problem:
                answer["refused"] = True
                answer["why_refused"] = str(problem)
            answer["best_round_trip_microseconds"] = clock.best_round_trip_microseconds
        elif step["op"] == "query":
            estimate = clock.host_time_for_unwrapped_device_microseconds(step["unwrapped"])
            if estimate is None:
                answer["estimate"] = None
            else:
                answer["estimate"] = {
                    # Repr, not a float: this is compared across two languages
                    # and a JSON round trip must not be what decides whether
                    # they agree.
                    "host_unix_seconds": repr(estimate.host_unix_seconds),
                    "uncertainty_microseconds": estimate.uncertainty_microseconds,
                    "host_time_iso8601": estimate.host_time_iso8601,
                }
        elif step["op"] == "forget":
            clock.forget_everything_observed()
        answer["has_an_estimate"] = clock.has_an_estimate
        out.append({**step, "answer": answer})
    return out


def main() -> int:
    trace = run(programme())
    out = HERE / "daemon-rs" / "tests" / "clock_trace.json"
    out.write_text(json.dumps(trace, indent=1) + "\n")
    print(f"{len(trace)} operations -> {out.relative_to(HERE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
