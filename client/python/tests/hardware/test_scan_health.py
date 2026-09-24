# SPDX-License-Identifier: LGPL-3.0-or-later
"""What link traffic costs the scan, as a regression test rather than a report.

docs/operations/bringup.md §5 asks a person to hammer the link and see whether
`overruns` moves. It did, on the reference board, and the fix is in: the scan
runs in the timer ISR and the foreground holds the engine only while it handles
a command. The numbers below are what that left behind -- so this file's job is
to notice if the cost comes back.

The load is `ReadState`, which the daemon answers by asking the board for a
`state_report`: one command and its largest routine reply, per call, through the
same link a session uses. The daemon's own heartbeat pings the board as well,
so the figures here include it, as a rig's do.

Why the thresholds are not zero: the foreground still has to stop the ISR while
it handles a command, and a scan period that goes by with no scan in it is
counted rather than absorbed. The budgets sit at roughly twice the measured
figure. The measurements are in docs/operations/hardware.md.
"""

from __future__ import annotations

import time

from bench_rig import within_budget

LOAD = 200

#: Scan periods lost per state_report, measured on the reference board.
MAX_OVERRUNS_PER_STATE = 20

#: The longest run of missed scan periods, 4 ms at 10 kHz.
MAX_WORST_GAP = 40


def overruns(rig) -> int:
    return rig.client.read_state().scan.overruns


def test_state_reports_cost_the_scan_little_enough_to_keep_the_handoff_honest(timed):
    before = overruns(timed)
    started = time.monotonic()
    for _ in range(LOAD):
        timed.client.read_state()
    elapsed = time.monotonic() - started
    per_command = (overruns(timed) - before) / LOAD
    within_budget(
        timed,
        per_command <= MAX_OVERRUNS_PER_STATE,
        (
            f"{per_command:.1f} scan periods lost per state_report, over a budget of "
            f"{MAX_OVERRUNS_PER_STATE} ({LOAD} commands in {elapsed:.2f} s). The link is "
            "costing the scan again -- see the measurement at the top of firmware/src/main.cpp."
        ),
    )


def test_no_single_gap_runs_into_milliseconds(timed):
    """`worst_gap` is the tail the per-command average hides."""
    for _ in range(LOAD):
        timed.client.read_state()
    worst = timed.client.read_state().scan.worst_gap
    within_budget(
        timed,
        worst <= MAX_WORST_GAP,
        (
            f"worst_gap {worst} scan periods: something blocked the foreground for "
            f"about {worst / 10:.1f} ms in one go"
        ),
    )


def test_a_quiet_link_costs_the_scan_nothing(timed):
    """Time alone must not lose scans; only work does.

    Measured against the cost of one command rather than against zero, because
    the read that takes the measurement is itself one.
    """
    first = overruns(timed)
    second = overruns(timed)
    one_command = second - first
    time.sleep(2.0)
    quiet_window = overruns(timed) - second
    within_budget(
        timed,
        quiet_window <= one_command + 5,
        (
            f"{quiet_window} scan periods lost across two idle seconds, against "
            f"{one_command} for the command that measured it. The board is losing scans "
            "with nothing on the link but the heartbeat."
        ),
    )


def test_no_reply_ever_waited_for_the_wire(rig):
    """`tx_stalls` counts replies the outbound queue had no room for.

    Zero is the assertion, not a budget: the queue holds two of the longest
    frames the protocol allows, which is sized for request/response.
    """
    for _ in range(LOAD):
        rig.client.read_state()
    stalls = rig.client.read_state().scan.tx_stalls
    assert stalls == 0, (
        f"{stalls} replies had to wait for the wire (bringup.md §5 calls any "
        "non-zero value a finding)"
    )


def test_link_traffic_does_not_produce_bad_frames(rig):
    """The counters that would show framing breaking down under load.

    Both staying still across 200 commands is what says the round trips above
    were clean rather than merely fast.
    """
    before = rig.client.read_device().link
    for _ in range(LOAD):
        rig.client.read_state()
    after = rig.client.read_device().link
    assert before is not None and after is not None
    assert after.dropped_lines == before.dropped_lines
    assert after.bad_lines == before.bad_lines
