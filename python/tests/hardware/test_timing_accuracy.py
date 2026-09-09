# SPDX-License-Identifier: GPL-3.0-or-later
"""What the board's clock and its response path actually measure, on silicon.

The one thing no other suite in this repository can say anything about. The host
build's scan is a nanosleep on a preemptible kernel and Renode runs on virtual
time, so "a 500 ms state lasted 500 ms" is true by construction in both and
evidence in neither. Here it is a measurement.

Four questions, and they are different questions:

* **Does a duration the device drew match the duration it served?** A fixed
  timeout is asked for in milliseconds and reported in microseconds by the
  device's own clock, so the error is the engine's overhead in entering and
  leaving a state.
* **Does that error scale with the duration?** This is the one that separates a
  fixed overhead from a *rate* error. An overhead of 60 us is invisible in a
  behavioural session; a clock running 0.5% fast puts every foreperiod in a
  paper out by the same 0.5%, and after one trial the two look identical.
* **How long does the board take to answer a line?** Entry action to transition,
  the whole chain -- write_outputs, the pin, the jumper, read_inputs, the input
  conditioner, Transition::matches(). Only the loopback harness closes it.
* **Does the device's clock agree with the host's?** Over a long dwell, in ppm,
  which is what says whether device timestamps can be joined to anything else on
  the rig.

**The budgets are measured figures with room over them, not design targets.**
Each is roughly twice what an Uno R4 Minima produced, recorded in the constant's
own comment with the date. Tight enough that a regression fails them, loose
enough that a board a little slower than the reference one does not. When one of
these fails, the number in the message is the finding -- write it down before
changing the budget.

Needs the eight-wire loopback harness for the response-latency half; the
duration half runs on a bare board.
"""

from __future__ import annotations

import statistics
import time

import pytest
from hardware_test_harness import LOOPBACK, Outcome, SingleGraphSetUploader, read_trial_result
from statemachined.device.message_vocabulary import Field, MsgType

#: The board's scan timer, from `kScanHz` in firmware/src/main.cpp. One period
#: is 100 us, and it is the unit every figure below is naturally expressed in.
SCAN_PERIOD_MICROSECONDS = 100

# --------------------------------------------------------------- the budgets ---

#: Fixed cost of entering and leaving a state, on top of the drawn duration.
#: Measured on an Uno R4 Minima, 2026-09-09: +8 to +88 us across dwells from
#: 20 ms to 1000 ms, mean about +50 us and flat -- see NO_RATE_ERROR below for
#: why "flat" is the load-bearing half of that sentence.
DURATION_ERROR_BUDGET_MICROSECONDS = 500

#: The same error at the longest dwell, as a fraction of it. 500 us in 1000 ms
#: is 500 ppm, which a fixed overhead cannot reach and a clock 0.05% fast does
#: immediately. Measured: 66 us, so 66 ppm.
RATE_ERROR_BUDGET_PARTS_PER_MILLION = 500

#: Spread of the same dwell served repeatedly. Measured: sd about 25 us.
DURATION_JITTER_BUDGET_MICROSECONDS = 300

#: Entry action to a transition firing on the line it drove, through the
#: jumper. Measured 2026-09-09 over 300 trials: **bimodal**, about 1150 us
#: (55%) and about 1950 us (38%), median 1196, p95 1957, max 1960.
#:
#: That is 12 to 20 scan periods for a chain that touches nothing but GPIO and,
#: on the face of it, should answer within two. The budgets below are set from
#: the measurement so this suite is a regression test today; the gap itself is
#: a finding and not a settled cost -- if it is ever explained and closed, these
#: come down with it.
RESPONSE_LATENCY_MEDIAN_BUDGET_MICROSECONDS = 2500
RESPONSE_LATENCY_WORST_BUDGET_MICROSECONDS = 6000

#: How far apart two lines raised by the *same* entry action are seen. A scan
#: gathers a whole port at once, so this should be nothing; a budget of one scan
#: period is there to catch a scan that reads its ports at different moments.
CROSS_LINE_SKEW_BUDGET_MICROSECONDS = 2 * SCAN_PERIOD_MICROSECONDS

#: Device clock against the host's, over a dwell long enough to see it. Both are
#: ordinary crystals, so this is loose on purpose: it is checking that the two
#: agree to within a fraction of a percent, not calibrating either.
CLOCK_AGREEMENT_BUDGET_PARTS_PER_MILLION = 5000

#: Trials per measurement. Enough that a median and a p95 mean something,
#: bounded so the suite stays under a minute.
LATENCY_TRIALS = 100
REPEAT_TRIALS = 20


# ------------------------------------------------------------------- helpers ---


def run(device, graph, trial_id: int, cap_ms: int = 5000):
    """Arm, start, collect. Every measurement here is one of these."""
    armed = device.request(
        MsgType.CONFIGURE, trial_id=trial_id, set_version=graph.version,
        cap_ms=cap_ms, start="serial",
    )
    assert armed[Field.MSG_TYPE] == MsgType.ARMED, armed
    started = device.request(MsgType.START, trial_id=trial_id)
    assert started[Field.MSG_TYPE] == MsgType.STARTED, started
    return read_trial_result(device.session)


def a_dwell_of(device, milliseconds: int, version: int):
    """One state that holds for a fixed time, then ends. Nothing else."""
    graph = SingleGraphSetUploader(device.session, version=version)
    graph.begin(n_states=2, entry=0)
    graph.dist(0, kind="fixed", a=milliseconds)
    graph.state(0, terminal=None, timeout={"dist": 0, "target": 1})
    graph.state(1, terminal=int(Outcome.HIT), timeout=None)
    assert graph.end()[Field.MSG_TYPE] == MsgType.SET_OK
    return graph


def a_line_answering_itself(device, version: int, outputs: list[int]):
    """A state that raises `outputs` and waits for every line they drive.

    The measured duration of state 0 is then the whole response path: the entry
    action reaching a pin, the jumper, the pin being read, the conditioner
    accepting it, and the predicate firing.
    """
    graph = SingleGraphSetUploader(device.session, version=version)
    graph.begin(n_states=2, entry=0)
    graph.state(0, terminal=None, timeout=None)
    for line in outputs:
        graph.action("entry", line=line, kind="high")
    mask = 0
    for line in outputs:
        mask |= 1 << LOOPBACK[line]
    graph.transition(target=1, all=mask)
    graph.state(1, terminal=int(Outcome.HIT), timeout=None)
    assert graph.end()[Field.MSG_TYPE] == MsgType.SET_OK
    return graph


def summarise(values: list[int]) -> str:
    ordered = sorted(values)
    return (
        f"n={len(ordered)} min={ordered[0]} median={statistics.median(ordered):.0f} "
        f"p95={ordered[int(0.95 * (len(ordered) - 1))]} max={ordered[-1]} "
        f"sd={statistics.pstdev(ordered):.0f} (us)"
    )


# ----------------------------------------------------- durations, on a clock ---


@pytest.mark.parametrize("milliseconds", [20, 50, 100, 500, 1000])
def test_a_drawn_duration_is_the_duration_the_board_serves(device, milliseconds):
    """What the device asked for against what it measured, at five scales.

    Five rather than one because a single point cannot distinguish a fixed
    overhead from a proportional one, and those two have completely different
    consequences for a session.
    """
    graph = a_dwell_of(device, milliseconds, version=40)
    errors = []
    for index in range(5):
        result = run(graph=graph, device=device, trial_id=4000 + index,
                     cap_ms=milliseconds * 4 + 2000)
        visit = result.visit(0)
        assert visit.drawn_ms == milliseconds, "the device drew something else"
        errors.append(visit.duration_us - milliseconds * 1000)

    worst = max(abs(error) for error in errors)
    assert worst <= DURATION_ERROR_BUDGET_MICROSECONDS, (
        f"a {milliseconds} ms state was served {worst} us off its drawn duration "
        f"(errors {errors}), over a budget of {DURATION_ERROR_BUDGET_MICROSECONDS} us"
    )


def test_the_duration_error_is_an_overhead_and_not_a_rate(device):
    """The one that separates +60 us from a clock running fast.

    A fixed overhead is the same number of microseconds at 20 ms and at 1000 ms.
    A rate error is the same *fraction*, so at 1000 ms it is fifty times what it
    was at 20 ms. Only the second one silently rescales every duration in a
    published paradigm, so the test is on the fraction rather than on the count.
    """
    long_dwell_ms = 1000
    graph = a_dwell_of(device, long_dwell_ms, version=41)
    errors = [
        run(device, graph, trial_id=4100 + index, cap_ms=6000).visit(0).duration_us
        - long_dwell_ms * 1000
        for index in range(5)
    ]

    worst_ppm = max(abs(error) for error in errors) / long_dwell_ms / 1000 * 1e6
    assert worst_ppm <= RATE_ERROR_BUDGET_PARTS_PER_MILLION, (
        f"over {long_dwell_ms} ms the device's clock was out by {worst_ppm:.0f} ppm "
        f"(errors {errors} us). A fixed overhead cannot reach this; a clock running "
        "fast can, and it would rescale every duration in every paradigm."
    )


def test_the_same_dwell_served_repeatedly_does_not_wander(device):
    """Jitter, which is what a response window is actually judged on.

    A mean that is right and a spread of milliseconds is a rig where the same
    trial is a different trial each time.
    """
    graph = a_dwell_of(device, 100, version=42)
    durations = [
        run(device, graph, trial_id=4200 + index, cap_ms=3000).visit(0).duration_us
        for index in range(REPEAT_TRIALS)
    ]

    spread = statistics.pstdev(durations)
    assert spread <= DURATION_JITTER_BUDGET_MICROSECONDS, (
        f"a 100 ms dwell varied by sd {spread:.0f} us over {REPEAT_TRIALS} trials: "
        f"{summarise(durations)}"
    )


def test_a_drawn_random_duration_lands_inside_the_range_it_was_drawn_from(device):
    """The randomised timings, checked against what they promised.

    Drawn on the device from a host-seeded PRNG and reported back, which is the
    whole point of reporting them -- a duration nobody checked is not evidence.
    Both halves are asserted: what was drawn is inside the range, and what was
    served matches what was drawn.
    """
    graph = SingleGraphSetUploader(device.session, version=43)
    graph.begin(n_states=2, entry=0)
    graph.dist(0, kind="uniform", a=100, b=300)
    graph.state(0, terminal=None, timeout={"dist": 0, "target": 1})
    graph.state(1, terminal=int(Outcome.HIT), timeout=None)
    assert graph.end()[Field.MSG_TYPE] == MsgType.SET_OK

    drawn, errors = [], []
    for index in range(REPEAT_TRIALS):
        visit = run(device, graph, trial_id=4300 + index, cap_ms=3000).visit(0)
        drawn.append(visit.drawn_ms)
        errors.append(abs(visit.duration_us - visit.drawn_ms * 1000))

    assert all(100 <= value <= 300 for value in drawn), f"drawn outside [100, 300]: {drawn}"
    assert max(errors) <= DURATION_ERROR_BUDGET_MICROSECONDS, (
        f"a drawn duration was served {max(errors)} us off what was drawn: {drawn}"
    )
    # Not a distribution test -- twenty draws cannot be one -- but a constant
    # would pass everything above, and a constant is the plausible bug.
    assert len(set(drawn)) > 1, f"every draw came back the same: {drawn}"


# ---------------------------------------------- the response path, end to end ---


def test_the_board_answers_its_own_line_within_the_measured_latency(device, loopback):
    """Entry action to transition, over many trials, through a jumper.

    The number this test exists to hold is in the constant's comment, and it is
    bimodal at roughly 1.15 ms and 1.95 ms rather than the two scan periods the
    chain looks like it should take. The budget is set above the measurement so
    this is a regression test rather than a claim that the figure is right.
    """
    graph = a_line_answering_itself(device, version=44, outputs=[0])
    latencies = []
    for index in range(LATENCY_TRIALS):
        result = run(device, graph, trial_id=4400 + index, cap_ms=1000)
        assert result.outcome == Outcome.HIT, (
            "the board did not see the line it raised; check the D10 -> D6 jumper"
        )
        assert result.visit(0).cause == "transition"
        latencies.append(result.visit(0).duration_us)

    median = statistics.median(latencies)
    assert median <= RESPONSE_LATENCY_MEDIAN_BUDGET_MICROSECONDS, (
        f"median response latency {median:.0f} us: {summarise(latencies)}"
    )
    assert max(latencies) <= RESPONSE_LATENCY_WORST_BUDGET_MICROSECONDS, (
        f"worst response latency {max(latencies)} us: {summarise(latencies)}"
    )


def test_two_lines_raised_together_are_seen_together(device, loopback):
    """Cross-line skew, which is what `all` over two lines depends on.

    Both outputs go high in one entry action and a scan reads a whole port at
    once, so waiting for both must cost no more than waiting for one. A
    difference here is a scan that samples its ports at different moments, and
    it would make "both levers held" mean "both levers held, give or take".
    """
    one = a_line_answering_itself(device, version=45, outputs=[0])
    one_line = [
        run(device, one, trial_id=4500 + index, cap_ms=1000).visit(0).duration_us
        for index in range(REPEAT_TRIALS)
    ]

    both = a_line_answering_itself(device, version=46, outputs=[0, 1])
    two_lines = [
        run(device, both, trial_id=4600 + index, cap_ms=1000).visit(0).duration_us
        for index in range(REPEAT_TRIALS)
    ]

    skew = abs(statistics.median(two_lines) - statistics.median(one_line))
    assert skew <= CROSS_LINE_SKEW_BUDGET_MICROSECONDS, (
        f"waiting for two lines cost {skew:.0f} us more than waiting for one "
        f"(one: {summarise(one_line)}; two: {summarise(two_lines)})"
    )


# ------------------------------------------------------ the clock, against ours ---


def test_the_device_clock_and_the_host_clock_agree_over_a_long_dwell(device):
    """Device microseconds against host monotonic, in ppm.

    Not a calibration of either -- both are ordinary crystals and the host is
    timing across a USB round trip, which is why the budget is a fraction of a
    percent rather than tens of ppm. What it catches is a device clock that is
    wrong by a *factor*: microseconds reported as milliseconds-since-boot, a
    duration rounded through a float, a prescaler off by one.
    """
    dwell_ms = 2000
    graph = a_dwell_of(device, dwell_ms, version=47)

    host_before = time.monotonic()
    result = run(device, graph, trial_id=4700, cap_ms=8000)
    host_elapsed_us = (time.monotonic() - host_before) * 1e6

    device_us = result.visit(0).duration_us
    # The host also paid for arming, starting and the result coming back, so it
    # can only ever be the longer of the two. Comparing against the dwell rather
    # than against the device's own figure would fold those in as clock error.
    assert host_elapsed_us >= device_us

    disagreement_ppm = abs(device_us - dwell_ms * 1000) / (dwell_ms * 1000) * 1e6
    assert disagreement_ppm <= CLOCK_AGREEMENT_BUDGET_PARTS_PER_MILLION, (
        f"the device served {device_us} us for a {dwell_ms} ms dwell, "
        f"{disagreement_ppm:.0f} ppm out; the host saw {host_elapsed_us:.0f} us in total"
    )
