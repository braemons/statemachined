# SPDX-License-Identifier: LGPL-3.0-or-later
"""What the performance suite records, and how it summarises a sample.

A module of its own rather than part of conftest.py: there is more than one
conftest on this suite's path, and `from conftest import` would pick whichever
came first.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

#: The board's scan timer, `kScanHz` in firmware/src/main.cpp.
SCAN_HZ = 10_000
SCAN_PERIOD_MICROSECONDS = 1_000_000 // SCAN_HZ


def summary(samples: list[float]) -> dict:
    """The distribution, not just the mean: tails are what a rig feels."""
    ordered = sorted(samples)

    def percentile(p: float) -> float:
        return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]

    return {
        "n": len(ordered),
        "mean": statistics.fmean(ordered),
        "p50": percentile(50),
        "p95": percentile(95),
        "p99": percentile(99),
        "max": ordered[-1],
        "min": ordered[0],
    }


@dataclass
class Figure:
    section: str
    name: str
    value: float | int | dict
    unit: str
    note: str = ""


@dataclass
class Report:
    figures: list[Figure] = field(default_factory=list)
    context: dict = field(default_factory=dict)

    def add(self, section: str, name: str, value, unit: str, note: str = "") -> None:
        self.figures.append(Figure(section, name, value, unit, note))


#: The one report a run fills, printed and written out by conftest.py.
REPORT = Report()
