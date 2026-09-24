# SPDX-License-Identifier: LGPL-3.0-or-later
"""What the board's clock and its response path actually measure, on silicon.

The one thing no other suite in this repository can say anything about. The host
build's scan is a nanosleep on a preemptible kernel, so "a 500 ms state lasted
500 ms" is true by construction there and evidence nowhere. Here it is a
measurement, taken from the `TrialResult` a session reads -- the board's own
microseconds, through the daemon.

Four questions, and they are different questions:

* **Does a duration the device drew match the duration it served?** A fixed
  timeout is asked for in milliseconds and reported in microseconds by the
  device's own clock, so the error is the engine's overhead in entering and
  leaving a state.
* **Does that error scale with the duration?** This separates a fixed overhead
  from a *rate* error. An overhead of 60 us is invisible in a behavioural
  session; a clock running 0.5% fast puts every foreperiod in a paper out by the
  same 0.5%, and after one trial the two look identical.
* **How long does the board take to answer a line?** Entry action to
  transition, the whole chain -- write_outputs, the pin, the jumper,
  read_inputs, the input conditioner, Transition::matches(). Only the loopback
  harness closes it.
* **Does the device's clock agree with the host's?** Over a long dwell, which
  is what says whether device timestamps can be joined to anything else on the
  rig.

**The budgets are measured figures with room over them, not design targets.**
Each is roughly twice what an Uno R4 Minima produced, recorded in the constant's
own comment with the date. When one of these fails, the number in the message is
the finding -- write it down before changing the budget.

Needs the eight-wire loopback harness for the response-latency half; the
duration half runs on a bare board.
"""

from __future__ import annotations

import statistics
import time

import pytest
from bench_rig import (
    SCAN_PERIOD_MICROSECONDS,
    a_dwell_of,
    a_line_answering_itself,
    within_budget,
)
from statemachined_client import TrialOutcome

# --------------------------------------------------------------- the budgets ---

#: Fixed cost of entering and leaving a state, on top of the drawn duration.
#: Measured on an Uno R4 Minima, 2026-09-09: +8 to +88 us across dwells from
#: 20 ms to 1000 ms, mean about +50 us and flat.
DURATION_ERROR_BUDGET_MICROSECONDS = 500

#: The same error at the longest dwell, as a fraction of it. 500 us in 1000 ms
#: is 500 ppm, which a fixed overhead cannot reach and a clock 0.05% fast does
#: immediately. Measured: 66 us, so 66 ppm.
RATE_ERROR_BUDGET_PARTS_PER_MILLION = 500

#: Spread of the same dwell served repeatedly. Measured: sd about 25 us.
DURATION_JITTER_BUDGET_MICROSECONDS = 300

#: Entry action to a transition firing on the line it drove, through the
#: jumper, measured **inside a running trial**. Measured 2026-09-09 over 60
#: trials: 100 us exactly -- one scan period, which is the floor. See
#: docs/operations/hardware.md, "Response latency and duration accuracy".
RESPONSE_LATENCY_BUDGET_MICROSECONDS = 3 * SCAN_PERIOD_MICROSECONDS

#: The same wait when the state is entered by the `start` command instead.
#: Measured 2026-09-09 over 60 trials: median 809 us, max 1561 -- what `start`
#: costs the foreground before the scan that begins the run.
START_ENTERED_LATENCY_BUDGET_MICROSECONDS = 4000

#: An entry state with no actions and a 0 ms timeout: its reported duration is
#: nothing but the gap between the `start` command arriving and the first scan
#: that could act on it. Measured 2026-09-09 over 60 trials: median 812 us.
START_COMMAND_OVERHEAD_BUDGET_MICROSECONDS = 2500

#: How far apart two lines raised by the same entry action are seen. A scan
#: gathers a whole port at once, so this should be nothing.
CROSS_LINE_SKEW_BUDGET_MICROSECONDS = 2 * SCAN_PERIOD_MICROSECONDS

#: Device clock against the host's, over a long dwell. Loose on purpose: it
#: checks that the two agree to within a fraction of a percent.
CLOCK_AGREEMENT_BUDGET_PARTS_PER_MILLION = 5000

LATENCY_TRIALS = 100
REPEAT_TRIALS = 20


def summarise(values: list[int]) -> str:
    ordered = sorted(values)
    return (
        f"n={len(ordered)} min={ordered[0]} median={statistics.median(ordered):.0f} "
        f"p95={ordered[int(0.95 * (len(ordered) - 1))]} max={ordered[-1]} "
        f"sd={statistics.pstdev(ordered):.0f} (us)"
    )


# ----------------------------------------------------- durations, on a clock ---


@pytest.mark.parametrize("milliseconds", [20, 50, 100, 500, 1000])
def test_a_drawn_duration_is_the_duration_the_board_serves(timed, milliseconds):
    """What the device asked for against what it measured, at five scales."""
    timed.use(a_dwell_of(f"dwell-{milliseconds}", milliseconds))
    errors = []
    for _ in range(5):
        result = timed.run(f"dwell-{milliseconds}", cap_milliseconds=milliseconds * 4 + 2000)
        visit = result.visits[0]
        assert visit.drawn_duration_ms == milliseconds, "the device drew something else"
        errors.append(visit.measured_duration_microseconds - milliseconds * 1000)

    worst = max(abs(error) for error in errors)
    within_budget(
        timed,
        worst <= DURATION_ERROR_BUDGET_MICROSECONDS,
        (
            f"a {milliseconds} ms state was served {worst} us off its drawn duration "
            f"(errors {errors}), over a budget of {DURATION_ERROR_BUDGET_MICROSECONDS} us"
        ),
    )


def test_the_duration_error_is_an_overhead_and_not_a_rate(timed):
    """The one that separates +60 us from a clock running fast."""
    long_dwell_ms = 1000
    timed.use(a_dwell_of("dwell-long", long_dwell_ms))
    errors = [
        timed.run("dwell-long", cap_milliseconds=6000).visits[0].measured_duration_microseconds
        - long_dwell_ms * 1000
        for _ in range(5)
    ]
    worst_ppm = max(abs(error) for error in errors) / long_dwell_ms / 1000 * 1e6
    within_budget(
        timed,
        worst_ppm <= RATE_ERROR_BUDGET_PARTS_PER_MILLION,
        (
            f"over {long_dwell_ms} ms the device's clock was out by {worst_ppm:.0f} ppm "
            f"(errors {errors} us). A fixed overhead cannot reach this; a clock running "
            "fast can, and it would rescale every duration in every paradigm."
        ),
    )


def test_the_same_dwell_served_repeatedly_does_not_wander(timed):
    """Jitter, which is what a response window is actually judged on."""
    timed.use(a_dwell_of("dwell-100", 100))
    durations = [
        timed.run("dwell-100", cap_milliseconds=3000).visits[0].measured_duration_microseconds
        for _ in range(REPEAT_TRIALS)
    ]
    spread = statistics.pstdev(durations)
    within_budget(
        timed,
        spread <= DURATION_JITTER_BUDGET_MICROSECONDS,
        (
            f"a 100 ms dwell varied by sd {spread:.0f} us over {REPEAT_TRIALS} trials: "
            f"{summarise(durations)}"
        ),
    )


def test_a_drawn_random_duration_lands_inside_the_range_it_was_drawn_from(rig):
    """The randomised timings, checked against what they promised.

    Drawn on the device from a host-seeded PRNG and reported back, which is the
    whole point of reporting them. Both halves are asserted: what was drawn is
    inside the range, and -- on a board -- what was served matches it.
    """
    rig.use(
        {
            "name": "uniform-dwell",
            "entry": "Dwell",
            "distributions": {
                "dwell": {"kind": "uniform", "minimum_ms": 100, "maximum_ms": 300}
            },
            "states": [
                {"name": "Dwell", "timeout": {"after": "dwell", "goto": "Done"}},
                {"name": "Done", "outcome": "HIT"},
            ],
        }
    )
    drawn, errors = [], []
    for _ in range(REPEAT_TRIALS):
        visit = rig.run("uniform-dwell", cap_milliseconds=3000).visits[0]
        drawn.append(visit.drawn_duration_ms)
        errors.append(
            abs(visit.measured_duration_microseconds - visit.drawn_duration_ms * 1000)
        )

    assert all(100 <= value <= 300 for value in drawn), f"drawn outside [100, 300]: {drawn}"
    # Not a distribution test -- twenty draws cannot be one -- but a constant
    # would pass everything above, and a constant is the plausible bug.
    assert len(set(drawn)) > 1, f"every draw came back the same: {drawn}"
    within_budget(
        rig,
        max(errors) <= DURATION_ERROR_BUDGET_MICROSECONDS,
        (f"a drawn duration was served {max(errors)} us off what was drawn: {drawn}"),
    )


# ---------------------------------------------- the response path, end to end ---


def test_the_board_answers_its_own_line_within_the_measured_latency(timed, loopback):
    """Entry action to transition, mid-trial, through a jumper.

    The honest figure for the response path, and the one a paradigm depends on:
    every response a subject makes arrives inside a running trial, not at the
    instant a `start` command lands.
    """
    timed.use(a_line_answering_itself("answer-mid-trial", [0], mid_trial=True))
    latencies = []
    for _ in range(LATENCY_TRIALS):
        result = timed.run("answer-mid-trial", cap_milliseconds=1000)
        assert result.outcome == TrialOutcome.HIT, (
            "the board did not see the line it raised; check the output 0 -> input 4 jumper"
        )
        assert result.visits[1].exit_cause == "transition"
        latencies.append(result.visits[1].measured_duration_microseconds)

    within_budget(
        timed,
        max(latencies) <= RESPONSE_LATENCY_BUDGET_MICROSECONDS,
        (f"pin-to-transition latency mid-trial: {summarise(latencies)}"),
    )


def test_a_state_entered_by_the_start_command_pays_the_links_overhead_on_top(timed, loopback):
    """The same wait, entered by `start`, and what `start` adds in front of it."""
    timed.use(a_line_answering_itself("answer-at-start", [0], mid_trial=False))
    latencies = []
    for _ in range(LATENCY_TRIALS):
        result = timed.run("answer-at-start", cap_milliseconds=1000)
        assert result.outcome == TrialOutcome.HIT
        latencies.append(result.visits[0].measured_duration_microseconds)

    within_budget(
        timed,
        max(latencies) <= START_ENTERED_LATENCY_BUDGET_MICROSECONDS,
        (f"a state entered by `start` answered its own line in {summarise(latencies)}"),
    )


def test_the_start_command_stamps_a_trial_about_a_millisecond_before_it_runs(timed):
    """The offset with the response path taken out of it altogether."""
    timed.use(a_dwell_of("dwell-0", 0))
    overheads = []
    for _ in range(REPEAT_TRIALS):
        result = timed.run("dwell-0", cap_milliseconds=1000)
        assert result.outcome == TrialOutcome.HIT
        overheads.append(result.visits[0].measured_duration_microseconds)

    within_budget(
        timed,
        max(overheads) <= START_COMMAND_OVERHEAD_BUDGET_MICROSECONDS,
        (
            f"`start` stamped the trial {summarise(overheads)} before the first scan "
            "that could act on it; see docs/operations/hardware.md"
        ),
    )


def test_two_lines_raised_together_are_seen_together(timed, loopback):
    """Cross-line skew, which is what `all` over two lines depends on."""
    timed.use(
        a_line_answering_itself("answer-one", [0], mid_trial=True),
        a_line_answering_itself("answer-two", [0, 1], mid_trial=True),
    )
    one_line = [
        timed.run("answer-one", cap_milliseconds=1000).visits[1].measured_duration_microseconds
        for _ in range(REPEAT_TRIALS)
    ]
    two_lines = [
        timed.run("answer-two", cap_milliseconds=1000).visits[1].measured_duration_microseconds
        for _ in range(REPEAT_TRIALS)
    ]
    skew = abs(statistics.median(two_lines) - statistics.median(one_line))
    within_budget(
        timed,
        skew <= CROSS_LINE_SKEW_BUDGET_MICROSECONDS,
        (
            f"waiting for two lines cost {skew:.0f} us more than waiting for one "
            f"(one: {summarise(one_line)}; two: {summarise(two_lines)})"
        ),
    )


# ------------------------------------------------------ the clock, against ours ---


def test_the_device_clock_and_the_host_clock_agree_over_a_long_dwell(timed):
    """Device microseconds against host monotonic.

    What it catches is a device clock that is wrong by a *factor*: microseconds
    reported as milliseconds, a duration rounded through a float, a prescaler
    off by one.
    """
    dwell_ms = 2000
    timed.use(a_dwell_of("dwell-2000", dwell_ms))
    host_before = time.monotonic()
    result = timed.run("dwell-2000", cap_milliseconds=8000)
    host_elapsed_us = (time.monotonic() - host_before) * 1e6

    device_us = result.visits[0].measured_duration_microseconds
    # The host also paid for arming, starting and the result coming back, so it
    # can only ever be the longer of the two.
    assert host_elapsed_us >= device_us

    disagreement_ppm = abs(device_us - dwell_ms * 1000) / (dwell_ms * 1000) * 1e6
    within_budget(
        timed,
        disagreement_ppm <= CLOCK_AGREEMENT_BUDGET_PARTS_PER_MILLION,
        (
            f"the device served {device_us} us for a {dwell_ms} ms dwell, "
            f"{disagreement_ppm:.0f} ppm out; the host saw {host_elapsed_us:.0f} us in total"
        ),
    )
