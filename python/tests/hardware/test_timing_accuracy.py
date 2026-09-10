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
#: jumper, measured **inside a running trial** -- the state is reached from a
#: dwell, so no command is anywhere near it. Measured 2026-09-09 over 60
#: trials: **100 us exactly**, min = max = median, sd 0.
#:
#: That is one scan period, which is the floor: the pin is driven at the end of
#: one scan and read at the start of the next, and there is nothing else in the
#: path. It was 223 us until the `visit` stream was moved out of the scan
#: interrupt -- building that line cost ~120 us, the scan overran its own tick,
#: and the next scan landed late. docs/operations/hardware.md has both halves.
#:
#: A budget of three periods rather than one: an exact figure asserted exactly
#: is a test that fails on the first board with a slightly different clock.
RESPONSE_LATENCY_BUDGET_MICROSECONDS = 300

#: The same wait when the state is entered by the `start` command instead.
#: Measured 2026-09-09 over 60 trials: median 809 us, max 1561.
#:
#: Not the response path -- that is 100 us, above, same graph and same jumper.
#: It is what `on_start` costs the foreground before the scan that begins the
#: run can happen: the trial now starts on that scan, together with its pins, so
#: this is honest about when the trial began rather than being an error in what
#: it reports. It was 1196 us and bimodal when the timestamp came off the link
#: instead.
START_ENTERED_LATENCY_BUDGET_MICROSECONDS = 4000

#: The same cost with the response path removed entirely: an entry state with
#: no actions and a 0 ms timeout, so its reported duration is nothing but the
#: gap between the `start` command arriving and the first scan that could act on
#: it. Measured 2026-09-09 over 60 trials: median 812 us, max 1562.
#:
#: This is the one to watch, and what is left in it is the device's own
#: foreground: parsing the command and building the `started` reply, during
#: which the engine is held. It is a fixed offset on the first state of every
#: trial and invisible against a foreperiod of tens of milliseconds. Shrinking
#: it further means splitting `receive()` so only dispatch happens inside the
#: hold -- see the note at the top of firmware/src/main.cpp.
START_COMMAND_OVERHEAD_BUDGET_MICROSECONDS = 2500

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


def a_line_answering_itself_mid_trial(device, version: int, outputs: list[int]):
    """The same wait, reached from a dwell rather than from the `start` command.

    The difference between this and `a_line_answering_itself` is the whole of
    the response-latency finding: identical pins, identical predicate, and 228
    us against 1196. Timing the second state of a trial rather than the first
    is what takes the link out of the measurement.
    """
    graph = SingleGraphSetUploader(device.session, version=version)
    graph.begin(n_states=3, entry=0)
    graph.dist(0, kind="fixed", a=20)
    graph.state(0, terminal=None, timeout={"dist": 0, "target": 1})
    graph.state(1, terminal=None, timeout=None)
    for line in outputs:
        graph.action("entry", line=line, kind="high")
    mask = 0
    for line in outputs:
        mask |= 1 << LOOPBACK[line]
    graph.transition(target=2, all=mask)
    graph.state(2, terminal=int(Outcome.HIT), timeout=None)
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
    """Entry action to transition, mid-trial, through a jumper.

    The honest figure for the response path, and the one a paradigm depends on:
    every response a subject makes arrives inside a running trial, not at the
    instant a `start` command lands. Measured at 228 us with a spread of one
    microsecond -- see the budget's comment.
    """
    graph = a_line_answering_itself_mid_trial(device, version=44, outputs=[0])
    latencies = []
    for index in range(LATENCY_TRIALS):
        result = run(graph=graph, device=device, trial_id=4400 + index, cap_ms=1000)
        assert result.outcome == Outcome.HIT, (
            "the board did not see the line it raised; check the D10 -> D6 jumper"
        )
        assert result.visit(1).cause == "transition"
        latencies.append(result.visit(1).duration_us)

    assert max(latencies) <= RESPONSE_LATENCY_BUDGET_MICROSECONDS, (
        f"pin-to-transition latency mid-trial: {summarise(latencies)}"
    )


def test_a_state_entered_by_the_start_command_pays_the_links_overhead_on_top(device, loopback):
    """The same wait, entered by `start`, and why the two figures differ.

    Not a second measurement of the response path -- it is the same path -- but
    of what `on_start` adds in front of it. It is asserted rather than merely
    written down because it is a real offset on the first state of every trial,
    and a change that made it worse would otherwise be invisible.
    """
    graph = a_line_answering_itself(device, version=48, outputs=[0])
    latencies = []
    for index in range(LATENCY_TRIALS):
        result = run(graph=graph, device=device, trial_id=4800 + index, cap_ms=1000)
        assert result.outcome == Outcome.HIT
        latencies.append(result.visit(0).duration_us)

    assert max(latencies) <= START_ENTERED_LATENCY_BUDGET_MICROSECONDS, (
        f"a state entered by `start` answered its own line in {summarise(latencies)}"
    )


def test_the_start_command_stamps_a_trial_about_a_millisecond_before_it_runs(device):
    """The offset with the response path taken out of it altogether.

    An entry state with no actions and a 0 ms timeout leaves on the first scan
    that sees it, so its whole reported duration is the gap between the
    timestamp `service_link()` took and the first `advance_trial()` after the
    EngineHold came down. No pin, no conditioner, no predicate.

    This is the measurement that located the finding, so it is the one that
    would notice it being fixed: if `on_start` ever stamps the trial where the
    trial starts, this drops to about one scan period.
    """
    graph = SingleGraphSetUploader(device.session, version=49)
    graph.begin(n_states=2, entry=0)
    graph.dist(0, kind="fixed", a=0)
    graph.state(0, terminal=None, timeout={"dist": 0, "target": 1})
    graph.state(1, terminal=int(Outcome.HIT), timeout=None)
    assert graph.end()[Field.MSG_TYPE] == MsgType.SET_OK

    overheads = []
    for index in range(REPEAT_TRIALS):
        result = run(device, graph, trial_id=4900 + index, cap_ms=1000)
        assert result.outcome == Outcome.HIT
        overheads.append(result.visit(0).duration_us)

    assert max(overheads) <= START_COMMAND_OVERHEAD_BUDGET_MICROSECONDS, (
        f"`start` stamped the trial {summarise(overheads)} before the first scan "
        "that could act on it; see docs/operations/hardware.md"
    )


def test_two_lines_raised_together_are_seen_together(device, loopback):
    """Cross-line skew, which is what `all` over two lines depends on.

    Both outputs go high in one entry action and a scan reads a whole port at
    once, so waiting for both must cost no more than waiting for one. A
    difference here is a scan that samples its ports at different moments, and
    it would make "both levers held" mean "both levers held, give or take".
    """
    one = a_line_answering_itself_mid_trial(device, version=45, outputs=[0])
    one_line = [
        run(device, one, trial_id=4500 + index, cap_ms=1000).visit(1).duration_us
        for index in range(REPEAT_TRIALS)
    ]

    both = a_line_answering_itself_mid_trial(device, version=46, outputs=[0, 1])
    two_lines = [
        run(device, both, trial_id=4600 + index, cap_ms=1000).visit(1).duration_us
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
