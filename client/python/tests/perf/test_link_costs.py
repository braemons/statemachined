# SPDX-License-Identifier: LGPL-3.0-or-later
"""What one command costs, on the wire and to the scan, with nothing else talking.

The board is opened directly (link.py), so every figure here is the firmware's:
the USB stack, COBS and the CRC, nanopb's decode and encode, and the session's
dispatch. firmware/src/main.cpp records the same measurements from before the
link was protobuf -- 3.0 overruns per ping, 9.1 per state_report -- and this is
the file that says what they are now.

**How a scan cost is taken.** `overruns` is a counter since boot, read out of a
`state_report`, so a figure is a difference: read, run N commands, read. The
second read is itself a command and lands in the difference, so N is large
enough that one command in N is noise (and the idle baseline says how much).

`worst_gap` is a maximum since boot, not per phase: it can only say something
when a phase sets a new record, and the report says which phase did.
"""

from __future__ import annotations

import time

import pytest
from figures import SCAN_HZ, SCAN_PERIOD_MICROSECONDS, summary
from link import pb

REPEATS = 500

#: The commands a session sends routinely, with nothing changed on the board by
#: sending them. `wiring`, `configure` and the graph frames change state and are
#: measured through the daemon instead.
COMMANDS = {
    "ping": lambda link: link.ping(),
    "state_request (-> state_report)": lambda link: link.state(),
    "pins in (-> pin_map)": lambda link: link.pins(pb.DIRECTION_IN),
    "pins out (-> pin_map)": lambda link: link.pins(pb.DIRECTION_OUT),
    "autorun query (-> autorun_ok)": lambda link: link.autorun_query(),
}


def _frame_bytes(message) -> int:
    return len(message.SerializeToString())


def test_the_scan_loses_nothing_on_an_idle_link(link, report):
    """The baseline every other figure is read against."""
    link.drain()
    before = link.scan()
    time.sleep(10.0)
    after = link.scan()
    lost = after.overruns - before.overruns
    report.add(
        "idle", "scan periods lost in 10 s idle (one state read included)", lost, "periods"
    )
    report.add(
        "idle",
        "share of scan periods lost",
        100.0 * lost / (10.0 * SCAN_HZ),
        "%",
    )
    if after.worst_gap > before.worst_gap:
        report.add("idle", "worst_gap set a new record", after.worst_gap, "periods")


@pytest.mark.parametrize("command", list(COMMANDS))
def test_what_one_command_costs(link, report, command):
    send = COMMANDS[command]
    link.drain()
    first = send(link)
    reply_bytes = _frame_bytes(first.reply)

    before = link.scan()
    round_trips = []
    unsolicited = 0
    for _ in range(REPEATS):
        exchange = send(link)
        round_trips.append(exchange.round_trip_s * 1e6)
        unsolicited += len(exchange.unsolicited)
    after = link.scan()

    per_command = (after.overruns - before.overruns) / REPEATS
    section = f"link: {command}"
    report.add(section, "reply size (protobuf, before framing)", reply_bytes, "bytes")
    report.add(section, "round trip, host-timed", summary(round_trips), "us")
    report.add(
        section,
        "scan periods lost per command",
        per_command,
        "periods",
        f"~{per_command * SCAN_PERIOD_MICROSECONDS:.0f} us of held engine",
    )
    if after.worst_gap > before.worst_gap:
        report.add(section, "worst_gap set a new record", after.worst_gap, "periods")
    assert after.tx_stalls == before.tx_stalls, "a reply had to wait for the wire"
    assert unsolicited == 0, f"{unsolicited} frames arrived unasked on an idle board"


def test_the_links_frame_counters_stayed_clean(link):
    """Everything above was a clean exchange rather than a fast failure."""
    report = link.state().reply.state_report
    assert report.dropped_lines == 0, f"{report.dropped_lines} frames dropped"
    assert report.bad_lines == 0, f"{report.bad_lines} frames did not decode"


@pytest.mark.parametrize("rate_hz", [20, 100, 250, 500, 0])
def test_sustained_command_rate(link, report, rate_hz):
    """Pings at a fixed rate for five seconds; 0 is as fast as the link answers.

    What a host polling the board costs it over time, which is the figure a
    rig's heartbeat and a console's polling actually add up to.
    """
    seconds = 5.0
    link.drain()
    before = link.scan()
    sent = 0
    started = time.perf_counter()
    next_at = started
    while time.perf_counter() - started < seconds:
        if rate_hz:
            now = time.perf_counter()
            if now < next_at:
                time.sleep(next_at - now)
            next_at += 1.0 / rate_hz
        link.ping()
        sent += 1
    elapsed = time.perf_counter() - started
    after = link.scan()

    label = f"{rate_hz} Hz" if rate_hz else "flat out"
    lost = after.overruns - before.overruns
    section = f"sustained pings, {label}"
    report.add(section, "commands per second achieved", sent / elapsed, "/s")
    report.add(section, "scan periods lost per second", lost / elapsed, "periods/s")
    report.add(section, "share of scan periods lost", 100.0 * lost / (elapsed * SCAN_HZ), "%")
    if after.worst_gap > before.worst_gap:
        report.add(section, "worst_gap set a new record", after.worst_gap, "periods")
    assert after.tx_stalls == before.tx_stalls, "a reply had to wait for the wire"
