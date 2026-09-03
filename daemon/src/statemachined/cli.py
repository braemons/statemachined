# SPDX-License-Identifier: LGPL-3.0-or-later
"""The command line, one subcommand per step of dev/BRINGUP.md.

Nothing here sends a command the operator did not ask for. In particular no
subcommand says `hello` behind somebody's back: on a bench board the greeting
ends demo mode for good until reset (BRINGUP.md §4), and a tool that did that
as a side effect of "just checking the state" would blank the very lamps
somebody was watching.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from .board import high_lines, word_bits
from .device.link import DEFAULT_BAUD, DEFAULT_TARGET, DEFAULT_TIMEOUT, Link
from .device.messages import ErrorCode, Field, MsgType
from .device.session import Session, Timeout, random_seed
from .device.wire import DeviceError, WireError, parse_reply, statemachined_line

SCAN_HZ_TARGET = 10_000  # dev/PLAN.md M3, and the number §4 is waiting on.


# --------------------------------------------------------------- printing ---


def note(text: str) -> None:
    """Commentary goes to stderr so that stdout stays pasteable.

    Flushed against stdout first, or a piped run interleaves a warning into the
    middle of the numbers it is about.
    """
    sys.stdout.flush()
    print(text, file=sys.stderr, flush=True)


def show_unsolicited(msg: dict) -> None:
    t = msg.get(Field.MSG_TYPE)
    if t == MsgType.LOG:
        note(f"  log [{msg.get('level', '?')}] {msg.get('message', '')}")
    elif t == MsgType.EVENT:
        note(f"  event us={msg.get('us')} word={msg.get('word')}")
    else:
        note(f"  {t} {json.dumps({k: v for k, v in msg.items() if k != Field.CRC})}")


def show_junk(line: str, why: str) -> None:
    note(f"  ignored ({why}): {line}")


def field(name: str, value) -> None:
    print(f"  {name:<14} {value}")


def seconds(us) -> str:
    return f"{us / 1e6:.1f} s" if isinstance(us, (int, float)) else "?"


KNOWN_STATE_KEYS = {
    *Field, "link_state", "graph", "trial_id", "running",
    "current_state", "up_us", "dropped_lines", "bad_lines", "io", "scan",
}


def print_state(report: dict, board: str | None, n_in: int, n_out: int) -> None:
    io = report.get("io", {})
    scan = report.get("scan", {})

    field("link_state", report.get("link_state"))
    g = report.get("graph", {})
    field(
        "graph",
        f"set v{g['set_version']}, {g['n_graphs']} graph(s), running {g['index']}"
        if g.get("has_set")
        else "none",
    )
    field("trial", f"{report.get('trial_id')}{' (running)' if report.get('running') else ''}")
    field("current_state", report.get("current_state"))
    field("up", seconds(report.get("up_us")))
    field("scan", f"{scan.get('hz')} Hz   overruns {scan.get('overruns')}"
                  f"   worst_gap {scan.get('worst_gap')}"
                  f"   tx_stalls {scan.get('tx_stalls')}")
    field("lines", f"dropped {report.get('dropped_lines')}   bad {report.get('bad_lines')}")
    for direction, count in (("in", n_in), ("out", n_out)):
        word = io.get(direction, 0)
        field(direction, f"{word_bits(word, count)}   "
                         f"high: {high_lines(board, direction, word, count)}")
    # A field this tool does not know about is a field the firmware grew since
    # it was written; unknown members are ignored by the protocol, not by the
    # person staring at a board.
    extra = {k: v for k, v in report.items() if k not in KNOWN_STATE_KEYS}
    if extra:
        field("other", json.dumps(extra))


def print_hello_ack(ack: dict) -> None:
    field("board", f"{ack.get('board')}   fw {ack.get('fw')}   proto {ack.get('proto')}")
    field("lines", f"{ack.get('n_input_lines')} in, {ack.get('n_output_lines')} out")
    hz = ack.get("scan_hz")
    verdict = ""
    if isinstance(hz, int):
        verdict = f"  ({'above' if hz >= SCAN_HZ_TARGET else 'BELOW'} the {SCAN_HZ_TARGET} Hz target)"
    field("scan_hz", f"{hz}{verdict}")
    field(
        "graph",
        f"set v{ack['set_version']}, {ack['n_graphs']} graph(s) (survived the reconnect)"
        if ack.get("has_set")
        else "none",
    )
    field("caps", json.dumps(ack.get("caps", {})))


# ---------------------------------------------------------------- helpers ---


def line_counts(session: Session, args) -> tuple[str | None, int, int]:
    """How many lines to render, and whose pinout to name them with.

    From `hello_ack` when this run said hello, otherwise from the flags --
    `state` deliberately does not greet the device, so it cannot ask.
    """
    ack = session.hello_ack or {}
    board = ack.get("board") or args.board
    return (
        board,
        int(ack.get("n_input_lines", args.lines)),
        int(ack.get("n_output_lines", args.lines)),
    )


def open_session(args) -> tuple[Link, Session]:
    link = Link(args.target, baud=args.baud, timeout=args.timeout)
    link.reset_input()
    return link, Session(link, on_unsolicited=show_unsolicited, on_junk=show_junk)


# --------------------------------------------------------------- commands ---


def cmd_hello(args, session: Session) -> int:
    seed = args.seed or random_seed()
    note("hello ends demo mode for good until the next reset.")
    ack = session.hello(seed=seed)
    print("hello_ack")
    field("seed", seed)
    print_hello_ack(ack)
    return 0


def cmd_state(args, session: Session) -> int:
    report = session.state()
    board, n_in, n_out = line_counts(session, args)
    print("state_report")
    print_state(report, board, n_in, n_out)
    return 0


def cmd_ping(args, session: Session) -> int:
    for i in range(args.count):
        started = time.monotonic()
        pong = session.ping()
        rtt_ms = (time.monotonic() - started) * 1e3
        print(f"  pong  up {seconds(pong.get('up_us'))}   round trip {rtt_ms:.1f} ms")
        if i + 1 < args.count:
            time.sleep(args.interval)
    return 0


def cmd_watch(args, session: Session) -> int:
    """§5: hold a switch, watch `io.in` change. Ctrl-C to stop."""
    board, n_in, n_out = line_counts(session, args)
    note("watching io; hold a switch and watch `in`. Ctrl-C to stop.")
    print(f"{'in':<10} {'out':<10} {'state':<10} scan")
    try:
        while True:
            report = session.state()
            io = report.get("io", {})
            scan = report.get("scan", {})
            print(
                f"{word_bits(io.get('in', 0), n_in):<10} "
                f"{word_bits(io.get('out', 0), n_out):<10} "
                f"{str(report.get('current_state')):<10} "
                f"overruns {scan.get('overruns')} worst_gap {scan.get('worst_gap')}",
                flush=True,
            )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_load(args, session: Session) -> int:
    """§5's real question: does link traffic cost the board scans?

    The design bets that a timer ISR which only counts, with `loop()` doing the
    scan, buys correctness at the price of jitter. This is the measurement that
    says whether the price is being paid: hammer the link with the cheapest
    command there is and see whether `overruns` moves. If it does, the known
    fix is a command handoff in firmware/src/main.cpp and nothing in core/.
    """
    before = session.state()
    note(f"sending {args.count} pings back to back...")
    started = time.monotonic()
    for _ in range(args.count):
        session.ping()
    elapsed = time.monotonic() - started
    after = session.state()

    b, a = before.get("scan", {}), after.get("scan", {})
    gained = (a.get("overruns", 0) or 0) - (b.get("overruns", 0) or 0)
    print(f"  {args.count} commands in {elapsed:.2f} s ({args.count / elapsed:.0f}/s)")
    print(f"  overruns   {b.get('overruns')} -> {a.get('overruns')}   ({gained:+d})")
    print(f"  worst_gap  {b.get('worst_gap')} -> {a.get('worst_gap')}")
    print(f"  dropped_lines {before.get('dropped_lines')} -> {after.get('dropped_lines')}"
          f"   bad_lines {before.get('bad_lines')} -> {after.get('bad_lines')}")
    if gained:
        note("FINDING: overruns climbed under link traffic. Write it down (BRINGUP.md §5).")
        return 1
    return 0


def cmd_report(args, session: Session) -> int:
    """The three numbers M3 wants, in a shape that pastes into HARDWARE.md.

    A measurement that stays in somebody's terminal is one the next person has
    to take again, so this prints markdown rather than prose. The scope numbers
    are not here because no amount of serial traffic can produce them.
    """
    seed = args.seed or random_seed()
    note("hello ends demo mode for good until the next reset.")
    ack = session.hello(seed=seed)
    before = session.state()
    for _ in range(args.count):
        session.ping()
    after = session.state()

    scan = after.get("scan", {})
    gained = (scan.get("overruns", 0) or 0) - (before.get("scan", {}).get("overruns", 0) or 0)
    board, n_in, n_out = line_counts(session, args)

    print(f"<!-- statemachined {__version__}, {board}, {args.target} -->")
    print()
    print("| Measurement | Value | Against |")
    print("|---|---|---|")
    print(f"| `scan_hz` (hello_ack) | {ack.get('scan_hz')} | {SCAN_HZ_TARGET} Hz target |")
    print(f"| `scan.overruns` after {args.count} pings | {scan.get('overruns')} "
          f"({gained:+d} during load) | zero |")
    print(f"| `scan.worst_gap` | {scan.get('worst_gap')} | zero |")
    print(f"| `scan.tx_stalls` | {scan.get('tx_stalls')} | zero |")
    print(f"| Link errors | dropped {after.get('dropped_lines')}, "
          f"bad {after.get('bad_lines')} | zero |")
    print(f"| Firmware | `{ack.get('fw')}` | the commit you believe you flashed |")
    print()
    print("Still to measure with a scope: D2 -> D10 latency, and the step dwell against 500 ms.")
    note("")
    note("io as of the last read:")
    print_state(after, board, n_in, n_out)
    return 0


def cmd_raw(args, session: Session) -> int:
    """Send a body without its closing brace; the CRC is added here.

    The escape hatch for everything this tool does not have a subcommand for --
    a graph upload by hand, a `configure`, a type that does not exist yet.
    """
    body = args.body.rstrip()
    if body.endswith("}"):
        raise SystemExit(
            "error: pass the body *without* its closing brace, e.g.\n"
            "       raw '{\"msg_type\":\"state\",\"message_id\":2'\n"
            "       The crc member is appended here and must be last."
        )
    session.link.write_line(statemachined_line(body))
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        line = session.link.read_line()
        if line is None:
            continue
        print(line)
        return 0
    note(f"no reply within {args.timeout:g}s")
    return 1


def cmd_monitor(args, session: Session) -> int:
    """Read and check whatever the device says, sending nothing.

    Deliberately silent on the wire: on a bench board this is the one way to
    look at the link without ending demo mode.
    """
    note("reading, sending nothing. Ctrl-C to stop.")
    try:
        while True:
            line = session.link.read_line()
            if line is None:
                continue
            try:
                parse_reply(line)
            except WireError as exc:
                note(f"  bad line ({exc})")
            print(line, flush=True)
    except KeyboardInterrupt:
        return 0


# ------------------------------------------------------------------- main ---


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="statemachined",
        description=(
            "Talk to a statemachined device. The steps are dev/BRINGUP.md; this is the "
            "instrument they ask for."
        ),
        epilog=(
            "TARGET is a device path (/dev/ttyACM0), a host:port for a device on a "
            "network, or any pyserial URL (socket://host:5000, rfc2217://host:5000). "
            "Set STATEMACHINED_TARGET to avoid typing it."
        ),
    )
    p.add_argument(
        "-t", "--target",
        default=os.environ.get("STATEMACHINED_TARGET", DEFAULT_TARGET),
        help="device path, host:port, or pyserial URL (default: %(default)s)",
    )
    p.add_argument(
        "-b", "--baud", type=int, default=DEFAULT_BAUD,
        help="serial only (default: %(default)s)",
    )
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="read timeout, seconds")
    p.add_argument("--seed", help="session seed, 1-16 hex digits (default: random)")
    p.add_argument(
        "--board",
        default="uno_r4_minima",
        help="pinout to label lines with when no hello_ack says (default: %(default)s)",
    )
    p.add_argument("--lines", type=int, default=8, help="lines to render when no hello_ack says")
    # The device refuses everything but `hello` before a session exists ("no
    # hello yet"), so `state` on a fresh board needs one -- but greeting it is
    # what ends demo mode, and that has to stay something somebody asked for
    # rather than something a diagnostic did on its way past.
    p.add_argument(
        "--hello",
        action="store_true",
        help="greet the device first, for commands the device refuses without a session. "
        "Ends demo mode",
    )
    p.add_argument("--version", action="version", version=__version__)

    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("hello", help="open a session; prints scan_hz. Ends demo mode")
    s.set_defaults(func=cmd_hello)

    s = sub.add_parser("state", help="one state_report: io, scan health, link counters")
    s.set_defaults(func=cmd_state)

    s = sub.add_parser("watch", help="poll state and print io as it changes")
    s.add_argument(
        "-i", "--interval", type=float, default=0.25,
        help="seconds (default: %(default)s)",
    )
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("ping", help="round trip and uptime")
    s.add_argument("-n", "--count", type=int, default=1)
    s.add_argument("-i", "--interval", type=float, default=0.5)
    s.set_defaults(func=cmd_ping)

    s = sub.add_parser("load", help="hammer the link, then check whether scans were lost")
    s.add_argument("-n", "--count", type=int, default=200)
    s.set_defaults(func=cmd_load)

    s = sub.add_parser("report", help="the M3 numbers, as markdown for dev/HARDWARE.md")
    s.add_argument("-n", "--count", type=int, default=200, help="pings of link load first")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("raw", help="send a hand-written body; the crc is added here")
    s.add_argument("body", help='message without its closing brace, e.g. \'{"msg_type":"state","message_id":2\'')
    s.set_defaults(func=cmd_raw)

    s = sub.add_parser("monitor", help="read lines and check their CRCs, sending nothing")
    s.set_defaults(func=cmd_monitor)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        link, session = open_session(args)
    except Exception as exc:  # pyserial raises several unrelated types
        note(f"error: cannot open {args.target}: {exc}")
        note("       On Linux the board is usually /dev/ttyACM0; if it is missing,")
        note("       double-tap reset to force the bootloader, and check group membership.")
        return 2
    try:
        with link:
            if args.hello and args.func not in (cmd_hello, cmd_report, cmd_monitor):
                note("hello ends demo mode for good until the next reset.")
                session.hello(seed=args.seed or random_seed())
            return args.func(args, session)
    except DeviceError as exc:
        note(f"device refused it: {exc}")
        if exc.code == ErrorCode.NOT_READY and exc.context == "hello":
            note("       The device answers nothing but `hello` before a session exists.")
            note("       Add --hello to greet it first -- which ends demo mode until reset.")
        return 1
    except Timeout as exc:
        note(f"timeout: {exc}")
        note("       Nothing was retried on purpose -- a silent retry would hide exactly")
        note("       the stall this is here to find. If the board is in demo mode it is")
        note("       still listening; if D13 is dark, the fault is before the link.")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
