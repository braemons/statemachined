# SPDX-License-Identifier: LGPL-3.0-or-later
"""The performance suite: numbers from a real board, written down.

tests/hardware asserts budgets and says pass or fail. This directory measures
and *reports*: every benchmark records what it saw, and the run ends with a
table on the terminal and a JSON file under build/perf/ that two runs -- two
firmware builds, two boards, two toolchains -- can be compared with. A few
sanity checks fail a benchmark (a frame the board could not read, a trial that
did not run); a slow figure never does. Deciding what is too slow is what the
numbers are for.

    make perf TARGET=/dev/ttyACM0

Two halves, never at once, because both want the port:

* test_link_*.py opens the board directly (link.py) -- no daemon, no heartbeat,
  no gRPC -- which is what isolates what the *firmware* costs: nanopb, the
  session, the USB stack.
* test_daemon_*.py starts a daemon in front of the board, as tests/hardware
  does, for what needs a committed graph and for what a session actually gets.
* test_profile.py reads a profiling build's own cycle counts -- where inside a
  command, a scan or a streaming trial the time goes. Flash the
  `uno_r4_minima_profile` env for it; on any other build it skips.

Stop anything else holding the board first; on a rig that is
`sudo systemctl stop statemachined`.
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

# tests/hardware's bench is reused as it is: the same daemon, the same line map,
# the same graph helpers.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hardware"))

from bench_rig import (
    BenchRig,
    scratch_directory,
    start_daemon,
    wait_until,
)
from figures import REPORT, Report
from link import Link

REPOSITORY = Path(__file__).resolve().parents[4]


@pytest.fixture(scope="session")
def report() -> Report:
    return REPORT


def _target(pytestconfig) -> str:
    target = pytestconfig.getoption("--target")
    if not target:
        pytest.skip("tests/perf needs --target: the board's device path")
    if target == "native" or "://" in target or ":" in target:
        pytest.skip("tests/perf measures a board on a cable; timing on `native` means nothing")
    return target


# --------------------------------------------------------------- the link half ---


@pytest.fixture(scope="module")
def link(pytestconfig, report):
    """The board, greeted directly. Module-scoped so the port is free again
    before the daemon half starts."""
    target = _target(pytestconfig)
    try:
        opened = Link(target)
    except OSError as error:
        pytest.exit(
            f"could not open {target}: {error}\n"
            "  Is a daemon holding it? On a rig: sudo systemctl stop statemachined",
            returncode=2,
        )
    ack = opened.hello_ack.hello_ack
    report.context.setdefault("board", ack.board)
    report.context.setdefault("firmware", ack.fw)
    report.context.setdefault("measured_scan_floor_hz", ack.scan_hz)
    report.context.setdefault("max_frame", ack.caps.max_frame)
    yield opened
    opened.close()


# ------------------------------------------------------------- the daemon half ---


@pytest.fixture(scope="module")
def bench(pytestconfig):
    target = _target(pytestconfig)
    scratch = scratch_directory()
    daemon = None
    try:
        daemon, client = start_daemon(target, scratch)
        if not wait_until(lambda: client.read_device().connected, timeout_s=15):
            pytest.exit(f"the daemon could not reach a board at {target}", returncode=2)
        lines = client.read_lines()
        yield BenchRig(
            client=client,
            board=client.read_device().board,
            native=False,
            board_input_pins=list(lines.board_input_pins),
            board_output_pins=list(lines.board_output_pins),
            scratch=scratch,
        )
        client.close()
    finally:
        if daemon is not None:
            daemon.terminate()
            try:
                daemon.wait(timeout=10)
            except Exception:
                daemon.kill()
        shutil.rmtree(scratch, ignore_errors=True)


# ------------------------------------------------------------------ the report ---


def _format(value) -> str:
    if isinstance(value, dict):
        return "  ".join(
            f"{key} {value[key]:.1f}"
            if isinstance(value[key], float)
            else f"{key} {value[key]}"
            for key in ("p50", "p95", "p99", "max", "mean", "n")
            if key in value
        )
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not REPORT.figures:
        return
    write = terminalreporter.write_line
    terminalreporter.section("statemachined performance")
    for key, value in REPORT.context.items():
        write(f"  {key}: {value}")
    section = None
    for figure in REPORT.figures:
        if figure.section != section:
            section = figure.section
            write("")
            write(f"[{section}]")
        note = f"   ({figure.note})" if figure.note else ""
        write(f"  {figure.name:<52} {_format(figure.value)} {figure.unit}{note}")

    out = config.getoption("--perf-out") or os.environ.get("STATEMACHINED_PERF_OUT")
    if not out:
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        out = REPOSITORY / "build" / "perf" / f"perf-{stamp}.json"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "taken": datetime.datetime.now(datetime.UTC).isoformat(),
        "host": platform.node(),
        "context": REPORT.context,
        "figures": [asdict(figure) for figure in REPORT.figures],
    }
    out.write_text(json.dumps(document, indent=2) + "\n")
    write("")
    write(f"  written to {out}")
