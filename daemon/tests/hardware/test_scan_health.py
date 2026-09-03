# SPDX-License-Identifier: GPL-3.0-or-later
"""What link traffic costs the scan, as a regression test rather than a report.

dev/BRINGUP.md §5 asks a person to hammer the link and see whether `overruns`
moves. It did, on the reference board, and the fix is in: the scan runs in the
timer ISR and the foreground holds the engine only while it parses a command.
The numbers below are what that left behind, measured -- so this file's job is
no longer to *discover* the cost but to notice if it comes back.

Why the thresholds are not zero. A scan period that goes by with no scan in it
is counted rather than absorbed, and the foreground still has to stop the ISR
for the ~372 us it spends inside HostLinkSession, or a command would be parsed
underneath a running trial. Three periods per command is that hold; it is
bounded by our own parse rather than by whatever the USB stack does, which is
the whole point of the change. The budgets here sit at roughly twice the
measured figure: tight enough that reverting the handoff (9.9/command) fails
them immediately, loose enough that a board a little slower than the reference
one does not.

The measurements themselves are in dev/HARDWARE.md, with the per-phase table.
"""

from __future__ import annotations

import time

from statemachined.device.messages import MsgType

#: Commands per measurement. Enough that a per-command figure is not dominated
#: by the two `state` reads that bracket it.
LOAD = 200

#: Measured 3.0 on an Uno R4 Minima at 10 kHz; 9.9 before the ISR handoff.
MAX_OVERRUNS_PER_PING = 8

#: Measured 9.1 for `state`, whose reply is ~290 bytes against a pong's ~65.
MAX_OVERRUNS_PER_STATE = 20

#: Measured 9 after the handoff, 103 before it. A gap in the hundreds means
#: something is blocking the foreground for milliseconds at a time.
MAX_WORST_GAP = 40


def overruns(report: dict) -> int:
    return report["scan"]["overruns"]


def test_pings_cost_the_scan_little_enough_to_keep_the_handoff_honest(device):
    before = device.state()
    started = time.monotonic()
    for _ in range(LOAD):
        device.request(MsgType.PING)
    elapsed = time.monotonic() - started
    after = device.state()

    per_command = (overruns(after) - overruns(before)) / LOAD
    assert per_command <= MAX_OVERRUNS_PER_PING, (
        f"{per_command:.1f} scan periods lost per ping, over a budget of "
        f"{MAX_OVERRUNS_PER_PING} ({LOAD} commands in {elapsed:.2f} s). The link is "
        "costing the scan again -- see the measurement at the top of firmware/src/main.cpp."
    )


def test_the_largest_reply_the_protocol_sends_routinely_is_still_bounded(device):
    """`state_report` is ~290 bytes against a pong's ~65.

    Worth measuring separately: before the handoff the cost tracked the reply's
    length, because the foreground blocked until the host drained every byte.
    Afterwards it should track the *parse*, which is the same work either way,
    so the two figures moving apart again is the signature of a regression.
    """
    before = device.state()
    for _ in range(LOAD):
        device.state()
    after = device.state()

    per_command = (overruns(after) - overruns(before)) / LOAD
    assert per_command <= MAX_OVERRUNS_PER_STATE, (
        f"{per_command:.1f} scan periods lost per state_report, over a budget of "
        f"{MAX_OVERRUNS_PER_STATE}"
    )


def test_no_single_gap_runs_into_milliseconds(device):
    """`worst_gap` is the tail the per-command average hides.

    An average of three with a worst case of two hundred is a different board
    from an average of three with a worst case of nine -- the first drops a
    response window occasionally, which is exactly the failure that would never
    be reproduced.
    """
    for _ in range(LOAD):
        device.request(MsgType.PING)
    worst = device.state()["scan"]["worst_gap"]
    assert worst <= MAX_WORST_GAP, (
        f"worst_gap {worst} scan periods: something blocked the foreground for "
        f"about {worst / 10:.1f} ms in one go"
    )


def test_a_quiet_link_costs_the_scan_nothing(device):
    """Time alone must not lose scans; only work does.

    Measured against the cost of one command rather than against zero, because
    the read that takes the measurement is itself a command. If sitting idle
    for two seconds cost more than the single `state` that ends the window, the
    scan would be losing periods to something that is not the link at all --
    a timer that does not fire, or an ISR that overruns its own period.
    """
    first = device.state()
    second = device.state()
    one_command = overruns(second) - overruns(first)

    time.sleep(2.0)
    third = device.state()
    quiet_window = overruns(third) - overruns(second)

    assert quiet_window <= one_command + 5, (
        f"{quiet_window} scan periods lost across two idle seconds, against "
        f"{one_command} for the command that measured it. The board is losing scans "
        "with nothing on the link."
    )


def test_no_reply_ever_waited_for_the_wire(device):
    """`tx_stalls` counts replies the outbound queue had no room for.

    Zero is the assertion, not a budget. The queue holds two of the longest
    lines the protocol allows, which is sized for request/response -- one
    command in flight, one reply -- so a stall means either the host stopped
    reading or something is emitting faster than request/response, and both are
    findings rather than degrees of slowness.
    """
    for _ in range(LOAD):
        device.request(MsgType.PING)
    scan = device.state()["scan"]
    assert scan["tx_stalls"] == 0, (
        f"{scan['tx_stalls']} replies had to wait for the wire (BRINGUP.md §5 "
        "calls any non-zero value a finding)"
    )


def test_link_traffic_does_not_produce_bad_lines(device):
    """The counters that would show framing breaking down under load.

    `dropped_lines` and `bad_lines` are the device's own view of the link. Both
    staying still across 200 commands is what says the round trips above were
    clean rather than merely fast.
    """
    before = device.state()
    for _ in range(LOAD):
        device.request(MsgType.PING)
    after = device.state()

    assert after["dropped_lines"] == before["dropped_lines"]
    assert after["bad_lines"] == before["bad_lines"]
