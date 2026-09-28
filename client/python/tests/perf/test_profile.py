# SPDX-License-Identifier: LGPL-3.0-or-later
"""Where the board's time goes, from inside: a profiling build's cycle counts.

Needs the `uno_r4_minima_profile` firmware (firmware/core/profile/profile.h);
on any other build every test here skips, saying so.

**What matters is the time a trial loses.** A scan period lost while nothing is
running costs nothing; one lost while a trial runs is a response window
measured on a clock that skipped. So the headline figures are the in-trial ones:
`hold (trial)` -- how long one command stops the scan while a trial runs -- and
what that hold is made of, and `scan (trial)` -- how much of its 100 us period
one scan uses, which is the headroom a bigger graph eats.

**How the set gets there.** Trials need a committed graph set, and compiling
one is the daemon's job, not this suite's. So a daemon commits the set and is
stopped, and the link is opened directly: a committed set survives a reconnect
(tests/hardware/test_trial_lifecycle.py holds the board to that).

Spans nest (profile.h). The hold is only the handler: the frame reader, the
decode, clearing the reply and encoding it all run outside it, where the scan
preempts them -- so those spans include the scans that interrupted them, and
the hold is what a trial pays.
"""

from __future__ import annotations

import time

import pytest
from bench_rig import BenchRig, scratch_directory, start_daemon, wait_until
from figures import SCAN_PERIOD_MICROSECONDS
from graphs import a_ping_pong, a_wait_with_transitions
from link import Link

#: profile::Span, in wire order.
SPANS = [
    "scan (idle)",
    "scan (trial)",
    "hold (idle)",
    "hold (trial)",
    "link_read",
    "frame_read",
    "rx_reset",
    "decode",
    "dispatch",
    "compose",
    "encode",
    "frame_write",
    "drain_outbound",
    "drain_tx",
]

#: As many transitions as the set has room for beside the ping-pong's two:
#: the reference board holds 64 in a whole set.
MANY_TRANSITIONS = 61

#: The set the daemon commits, by graph index.
WAIT_MANY = 0
PING_PONG_1MS = 1
WAIT_0 = 2

REPEATS = 300


def _commit_the_set(target: str) -> None:
    scratch = scratch_directory()
    daemon, client = start_daemon(target, scratch)
    try:
        if not wait_until(lambda: client.read_device().connected, timeout_s=15):
            pytest.exit(f"the daemon could not reach a board at {target}", returncode=2)
        lines = client.read_lines()
        bench = BenchRig(
            client=client,
            board=client.read_device().board,
            native=False,
            board_input_pins=list(lines.board_input_pins),
            board_output_pins=list(lines.board_output_pins),
            scratch=scratch,
        )
        bench.use(
            a_wait_with_transitions("profile-wait-many", MANY_TRANSITIONS, dwell_ms=60_000),
            a_ping_pong("profile-ping-pong", 1),
            a_wait_with_transitions("profile-wait-0", 0, dwell_ms=60_000),
        )
        client.close()
    finally:
        daemon.terminate()
        daemon.wait(timeout=10)


@pytest.fixture(scope="module")
def board(pytestconfig, report):
    target = pytestconfig.getoption("--target")
    if not target or target == "native" or ":" in target:
        pytest.skip("tests/perf needs --target: the board's device path")
    with Link(target) as probe:
        if not probe.profile().enabled:
            pytest.skip("not a profiling build: flash the uno_r4_minima_profile env to profile")
    _commit_the_set(target)
    link = Link(target)
    if not link.hello_ack.hello_ack.has_set:
        link.close()
        pytest.fail("the committed set did not survive the daemon disconnecting")
    first = link.profile()
    report.context["profile"] = {
        "cycles_per_second": first.cycles_per_second,
        "host_message_struct_bytes": first.host_message_bytes,
        "device_message_struct_bytes": first.device_message_bytes,
    }
    yield link
    link.close()


# ------------------------------------------------------------------ reading ---


class Trial:
    """A trial on the committed set, cancelled on the way out."""

    next_id = 50_000

    def __init__(self, link: Link, graph_index: int, cap_ms: int = 120_000):
        Trial.next_id += 1
        self.link = link
        self.trial_id = Trial.next_id
        armed = link.configure(self.trial_id, graph_index=graph_index, cap_ms=cap_ms)
        assert armed.reply.WhichOneof("body") == "armed", armed.reply
        started = link.start(self.trial_id)
        assert started.reply.WhichOneof("body") == "started", started.reply

    def __enter__(self) -> Trial:
        return self

    def __exit__(self, *_) -> None:
        cancelled = self.link.cancel(self.trial_id)
        # The result can overtake the cancel's own acknowledgement.
        if not any(
            frame.WhichOneof("body") == "result_end"
            and frame.result_end.trial_id == self.trial_id
            for frame in cancelled.unsolicited
        ):
            self.link.wait_for_result(self.trial_id, timeout_s=10)
        self.link.drain()


def _read(link: Link) -> tuple[dict, object]:
    """The profile as {span name: stats}, in microseconds."""
    raw = link.profile()
    per_us = 1e6 / raw.cycles_per_second
    spans = {}
    for span in raw.spans:
        if span.count == 0:
            continue
        spans[SPANS[span.span]] = {
            "n": span.count,
            "mean": span.total_cycles / span.count * per_us,
            "max": span.max_cycles * per_us,
            "total": span.total_cycles * per_us,
        }
    return spans, raw


def _report(report, section: str, spans: dict, *, per: int | None = None) -> None:
    for name in SPANS:
        if name not in spans:
            continue
        s = spans[name]
        value = {"mean": s["mean"], "max": s["max"], "n": s["n"]}
        note = ""
        if per:
            note = f"{s['total'] / per:.1f} us per command"
        report.add(section, name, value, "us", note)


def _outside_the_hold(spans: dict, commands: int) -> float:
    """What a command costs the foreground with the scan free to preempt it:
    the frame, the decode, clearing the reply and encoding it."""
    return (
        sum(
            spans[name]["total"]
            for name in ("frame_read", "rx_reset", "decode", "compose", "encode", "frame_write")
            if name in spans
        )
        / commands
    )


# -------------------------------------------------------------- measurements ---


@pytest.mark.parametrize(
    "command", ["ping", "state_request (-> state_report)"], ids=["ping", "state"]
)
@pytest.mark.parametrize("during", ["idle", "trial"])
def test_where_one_commands_time_goes(board, report, command, during):
    send = board.ping if command == "ping" else board.state
    section = f"profile: {command}, {'during a trial' if during == 'trial' else 'no trial'}"

    def run() -> None:
        board.profile(reset=True)
        for _ in range(REPEATS):
            send()

    if during == "trial":
        with Trial(board, WAIT_MANY):
            time.sleep(0.2)
            run()
            spans, raw = _read(board)
        report.add(
            section,
            "scan periods lost while the trial ran, per command",
            raw.overruns_in_trial / REPEATS,
            "periods",
            f"worst gap in trial {raw.worst_gap_in_trial}",
        )
    else:
        run()
        spans, raw = _read(board)

    _report(report, section, spans, per=REPEATS)
    report.add(
        section,
        "work outside the hold, per command (preemptible by the scan)",
        _outside_the_hold(spans, REPEATS),
        "us",
    )


@pytest.mark.parametrize(
    ("graph_index", "label"),
    [(WAIT_0, "0 transitions"), (WAIT_MANY, f"{MANY_TRANSITIONS} transitions")],
)
def test_how_much_of_its_period_a_scan_uses(board, report, graph_index, label):
    """With the link quiet: the scan alone, which is the headroom a graph eats."""
    with Trial(board, graph_index):
        time.sleep(0.2)
        board.profile(reset=True)
        time.sleep(3.0)
        spans, raw = _read(board)
    scan = spans["scan (trial)"]
    report.add(
        "profile: one scan during a trial, link quiet",
        f"{label}: scan",
        {"mean": scan["mean"], "max": scan["max"], "n": scan["n"]},
        "us",
        f"of a {SCAN_PERIOD_MICROSECONDS} us period; max is {100 * scan['max'] / SCAN_PERIOD_MICROSECONDS:.0f}%",
    )
    report.add(
        "profile: one scan during a trial, link quiet",
        f"{label}: scan periods lost",
        raw.overruns_in_trial,
        "periods",
    )


def test_where_a_streaming_trials_time_goes(board, report):
    """A state change every millisecond for a second: 1000 `visit` frames, then
    the result, all built in loop() and drained to USB."""
    board.profile(reset=True)
    Trial.next_id += 1
    trial_id = Trial.next_id
    board.configure(trial_id, graph_index=PING_PONG_1MS, cap_ms=1000)
    board.start(trial_id)
    frames = board.wait_for_result(trial_id, timeout_s=10)
    board.drain()
    spans, raw = _read(board)
    section = "profile: a state change every 1 ms, for 1 s"
    report.add(section, "frames the board sent", len(frames), "frames")
    report.add(
        section,
        "scan periods lost while the trial ran",
        raw.overruns_in_trial,
        "periods",
        f"worst gap in trial {raw.worst_gap_in_trial}",
    )
    _report(report, section, spans)
