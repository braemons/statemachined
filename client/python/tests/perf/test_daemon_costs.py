# SPDX-License-Identifier: LGPL-3.0-or-later
"""What a graph costs the board, and what a session gets through the daemon.

The other half of the suite: a daemon in front of the board, as on a rig,
because a graph has to be committed before it can cost anything and the daemon
is what compiles and uploads one. The daemon's heartbeat is in every figure
here; each measurement has a baseline taken the same way so it cancels.

Needs the eight-wire loopback harness for the response-latency figure only.
"""

from __future__ import annotations

import json
import time

import pytest
from bench_rig import a_line_answering_itself
from figures import SCAN_HZ, SCAN_PERIOD_MICROSECONDS, summary
from graphs import a_chain, a_ping_pong, a_wait_with_transitions
from statemachined_client import TrialOutcome


def _overruns(bench) -> int:
    return bench.client.read_state().scan.overruns


# -------------------------------------------------------------- measurements ---


def test_what_the_daemon_adds_to_a_state_read(bench, report):
    """`read_state` through gRPC, against the same command on the bare link."""
    samples = []
    for _ in range(300):
        started = time.perf_counter()
        bench.client.read_state()
        samples.append((time.perf_counter() - started) * 1e6)
    report.add("daemon", "read_state round trip (gRPC + daemon + link)", summary(samples), "us")

    health = []
    for _ in range(300):
        started = time.perf_counter()
        bench.client.read_health()
        health.append((time.perf_counter() - started) * 1e6)
    report.add("daemon", "read_health round trip (gRPC + daemon only)", summary(health), "us")


def test_the_daemons_heartbeat_costs_the_scan(bench, report):
    before = _overruns(bench)
    time.sleep(10.0)
    lost = _overruns(bench) - before
    report.add(
        "daemon",
        "scan periods lost per second, daemon attached and idle",
        lost / 10.0,
        "periods/s",
        "its heartbeat, plus one state read",
    )


@pytest.mark.parametrize("transitions", [0, 8, 32, 63])
def test_what_a_states_transitions_cost_the_scan(bench, report, transitions):
    """Every scan evaluates every transition of the current state.

    Measured in the middle of a long dwell, between two state reads, so the
    configure, the start and the result are all outside the window.
    """
    name = f"wait-{transitions}"
    bench.use(a_wait_with_transitions(name, transitions, dwell_ms=4000))
    trial_id = bench.next_trial_id()
    since = bench.client.read_state().newest_trace_entry_number
    bench.client.configure_trial(trial_id, graph=name, cap_milliseconds=6000)
    bench.client.start_trial(trial_id)
    time.sleep(0.5)
    first = bench.client.read_state()
    time.sleep(3.0)
    second = bench.client.read_state()
    bench.client.wait_for_trial(trial_id, timeout_s=10, since_entry_number=since)
    assert first.running and second.running, "the trial was not running across the window"
    per_second = (second.scan.overruns - first.scan.overruns) / 3.0
    report.add(
        "graph: transitions in the current state",
        f"{transitions:>2} transitions: scan periods lost per second",
        per_second,
        "periods/s",
        "compare with 0; the daemon's heartbeat is in all of them",
    )


@pytest.mark.parametrize("period_ms", [20, 5, 2, 1])
def test_what_streaming_visits_costs(bench, report, period_ms):
    """A `visit` frame per state change, as fast as a graph can change state.

    A second of hand-overs, then the result: the path buffer fills (255 rows),
    and the result frames go out in one burst at the end.
    """
    name = f"ping-pong-{period_ms}"
    bench.use(a_ping_pong(name, period_ms))
    before = bench.client.read_state().scan
    started = time.perf_counter()
    result = bench.run(name, cap_milliseconds=1000)
    elapsed = time.perf_counter() - started
    after = bench.client.read_state().scan

    section = f"graph: a state change every {period_ms} ms, for 1 s"
    report.add(section, "visits the board counted", result.total_visit_count, "visits")
    report.add(
        section,
        "path rows that came back",
        len(result.visits),
        "rows",
        "truncated" if result.path_was_truncated else "",
    )
    report.add(
        section,
        "scan periods lost, whole trial",
        after.overruns - before.overruns,
        "periods",
        "configure, start, visits, result and two state reads",
    )
    report.add(section, "tx_stalls", after.tx_stalls - before.tx_stalls, "stalls")
    report.add(section, "arm to result, host-timed", elapsed * 1e3, "ms")
    if after.worst_gap > before.worst_gap:
        report.add(section, "worst_gap set a new record", after.worst_gap, "periods")


@pytest.mark.parametrize("states", [2, 8, 16, 30])
def test_what_uploading_a_graph_costs(bench, report, states):
    """Committing a set: the daemon compiles it and sends it a frame at a time."""
    name = f"chain-{states}"
    document = {
        "name": "perf-suite",
        "description": "written by client/python/tests/perf",
        "board": "",
        "line_map": bench.line_map(),
        "graphs": [a_chain(name, states)],
    }
    bench.client.write_config("perf-suite", json.dumps(document))
    bench.client.load_config("perf-suite")
    before = bench.client.read_state().scan
    started = time.perf_counter()
    bench.client.open_session()
    elapsed = time.perf_counter() - started
    after = bench.client.read_state().scan
    # The next `use` must commit its own set, not trust this one.
    bench._loaded = ()

    section = "graph upload (open_session)"
    report.add(section, f"{states:>2} states: time to commit", elapsed * 1e3, "ms")
    report.add(
        section,
        f"{states:>2} states: scan periods lost",
        after.overruns - before.overruns,
        "periods",
    )


def test_how_fast_the_board_answers_its_own_line(bench, report):
    """Entry action to transition through a jumper, mid-trial: the response path."""
    bench.use(a_line_answering_itself("perf-answer", [0], mid_trial=True))
    latencies = []
    for _ in range(40):
        result = bench.run("perf-answer", cap_milliseconds=1000)
        if result.outcome != TrialOutcome.HIT:
            pytest.skip("the loopback harness is not wired (output 0 -> input 4)")
        latencies.append(result.visits[1].measured_duration_microseconds)
    report.add(
        "response path",
        "output raised -> input seen -> transition, mid-trial",
        summary(latencies),
        "us",
        f"one scan period is {SCAN_PERIOD_MICROSECONDS} us",
    )


def test_the_scan_rate_in_context(bench, report):
    """For reading everything above: what a lost period is worth."""
    report.add(
        "context",
        "scan timer",
        SCAN_HZ,
        "Hz",
        f"one period {SCAN_PERIOD_MICROSECONDS} us; 1% lost is {SCAN_HZ // 100} periods/s",
    )
