# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixtures for the suite that needs a board on the end of a cable.

Everything else in this repository is tested on the host or on an emulated
board, and both are real tests of the logic. This directory is the part that
cannot be: it runs against silicon, one device, in one order, and the fixtures
below exist to make that stateful device behave like something a test suite can
use -- greeted once, left idle between tests, and reported honestly when it is
not there at all.

Three consequences of "one real device" are worth stating, because they shape
every file here:

* **RequestResponseSession scope, not function scope.** A `hello` costs a round trip and, on a
  bench board, takes the rig from it; doing it per test would say nothing new
  twenty times. The greeting happens once and the tests share it.
* **Tests must not leave a trial running.** The device refuses a graph upload
  while one is (`busy`), so one careless test would fail the next four with an
  unrelated error. The `device` fixture checks after every test and says which
  test left it that way.
* **Some things can only be asked once per reset.** "Refuses commands before
  hello" is the clear case: after the greeting there is no way back without
  power-cycling the board. It is probed once, before the greeting, and the test
  reads the probe.
"""

from __future__ import annotations

import pytest
import serial
from statemachined.device.serial_link import DEFAULT_BAUD, DEFAULT_TARGET, SerialLink
from statemachined.device.message_vocabulary import Field, MsgType
from statemachined.device.request_response_session import RequestResponseSession, NoReplyInTime, random_seed
from statemachined.device.message_framing import DeviceRefusedTheCommand

from hardware_test_harness import (
    LOOPBACK,
    TRIAL_OUTPUT_LINE,
    LOOPBACK_PINS,
    Device,
    LinkState,
    SingleGraphSetUploader,
    wiring_instructions,
)

def pytest_addoption(parser):
    # `--target` is registered in python/tests/conftest.py, not here: `runs/`
    # takes it too, and pytest registers an option once per run rather than once
    # per conftest -- so defining it in both is a collision that kills
    # collection for anything gathering both suites.
    parser.addoption("--baud", type=int, default=DEFAULT_BAUD, help="serial only")
    parser.addoption(
        "--read-timeout",
        type=float,
        default=1.0,
        help="per-read timeout in seconds; the suite's own deadlines sit above it",
    )


@pytest.fixture(scope="session")
def target(pytestconfig) -> str:
    """The board to run against; the reference path when nobody said.

    This suite needs a board by definition, so "no --target" means the usual
    one rather than "do not run" -- which is the opposite of what it means to
    `runs/`, and why the default lives in neither conftest.
    """
    return pytestconfig.getoption("--target") or DEFAULT_TARGET


@pytest.fixture(scope="session")
def link(pytestconfig, target) -> SerialLink:
    """The one open port, for the whole run.

    A device that is not there ends the run rather than failing every test in
    turn: forty identical "could not open /dev/ttyACM0" failures bury the one
    sentence that says what to do about it.
    """
    try:
        link = SerialLink(
            target,
            baud=pytestconfig.getoption("--baud"),
            timeout=pytestconfig.getoption("--read-timeout"),
        )
    except (OSError, serial.SerialException, ValueError) as exc:
        pytest.exit(
            f"cannot open {target}: {exc}\n"
            "  On Linux the reference board is usually /dev/ttyACM0. If it is missing,\n"
            "  double-tap reset to force the bootloader, and check group membership\n"
            "  (dialout/uucp). For a board on a network: make test-hardware TARGET=host:5000",
            returncode=2,
        )
    # A bench board has been talking to nobody, and a serial monitor left open
    # earlier can leave a partial line in the driver -- which would otherwise
    # show up as one spurious framing complaint in whichever test read first.
    link.reset_input()
    yield link
    link.close()


@pytest.fixture(scope="session")
def board(link) -> Device:
    """The one `RequestResponseSession` for the whole run, greeted or not.

    There is exactly one, and that is load-bearing rather than tidy. The device
    keeps a one-deep duplicate-command guard keyed on `message_id`, so a second
    `RequestResponseSession` on the same link -- with its own counter, also starting at 1 --
    does not merely renumber things: its first command is read as a *resend* of
    the first session's, and answered from the cache. That is the guard working
    exactly as PROTOCOL.md §1.2 specifies, and it cost an afternoon here, where
    a probe and a greeting each opened their own session and `hello` came back
    as the probe's `state_report`.
    """
    return Device(link, RequestResponseSession(link))


@pytest.fixture(scope="session")
def pre_hello_probe(board) -> dict | None:
    """What the device says to a command sent before any `hello`.

    Asked once, first, because the answer stops existing the moment the board
    is greeted: `hello` moves the session out of `Greeting` and there is no way
    back short of a reset. Returns the refusal, or None if this board had
    already been greeted since it was last reset -- which is the normal case on
    a second run of the suite, and is a skip rather than a failure.
    """
    try:
        board.state(timeout=3.0)
    except DeviceRefusedTheCommand as exc:
        return {"code": exc.code, "context": exc.context, "message": exc.message}
    except NoReplyInTime:
        return None
    return None


@pytest.fixture(scope="session")
def greeted(board, pre_hello_probe) -> Device:
    """The board, greeted once. Depends on the probe so it cannot run first."""
    device = board
    seed = random_seed()
    try:
        ack = device.session.hello(seed=seed, timeout=5.0)
    except (NoReplyInTime, DeviceRefusedTheCommand) as exc:
        pytest.exit(f"the board did not answer hello: {exc}", returncode=2)
    device.seed = seed
    device.ack = ack
    _give_it_a_wiring_of_our_own(device, ack)
    return device


def _give_it_a_wiring_of_our_own(device, ack: dict) -> None:
    """Enable every line, rather than inheriting whatever the last host left.

    A board remembers its wiring across a greeting -- deliberately, since that
    is what lets it come up self-driving after a power cut -- and `enable` is a
    *mask*. So a rig whose line map named only four inputs leaves the other four
    switched off, and the next suite to run reads them as permanently low.

    That is not hypothetical: it is what `python/tests/runs/` does to this board
    when it is pointed at one, and the symptom was this suite reporting four of
    the eight loopback wires missing while they were all firmly in their holes.
    A test suite that silently depends on the last thing anybody ran is a test
    suite that passes or fails by history, so this one states what it needs.
    """
    inputs = int(ack.get("n_input_lines", 0))
    every_input_line = (1 << inputs) - 1
    device.request(
        MsgType.WIRING,
        invert=0,
        enable=every_input_line,
        safe=0,
        debounce_ms=[],
    )


@pytest.fixture
def device(greeted) -> Device:
    """The greeted board, checked back in idle after every test.

    Idle is what the *next* test needs: the device refuses a graph upload while
    a trial is armed or running (`busy`), so one test that walks away from an
    armed trial fails every later upload with an error naming the wrong file.

    The two untidy states are not treated alike. Leaving a trial **armed** is
    ordinary -- several tests below arm one to see a refusal -- so it is undone
    quietly. Leaving one **running** is not: something either found a bug or
    forgot a cancel, and silently cleaning that up would hide whichever it was.
    """
    greeted.stray.clear()
    greeted.junk.clear()
    yield greeted

    greeted.settle(0.1)
    try:
        report = greeted.state()
    except (NoReplyInTime, DeviceRefusedTheCommand):
        return  # the test itself will have failed; nothing to add here

    link_state = report.get("link_state")
    if link_state == LinkState.RUNNING:
        trial_id = report.get("trial_id")
        try:
            greeted.request(MsgType.CANCEL, trial_id=trial_id)
            greeted.settle(0.5)  # the result burst a cancel produces
        except (NoReplyInTime, DeviceRefusedTheCommand):
            pass
        pytest.fail(
            f"this test left trial {trial_id} running. It has been cancelled so the rest "
            "of the run is still meaningful, but every later graph upload would "
            "otherwise have been refused as `busy`."
        )
    elif link_state == LinkState.ARMED:
        # `hello` is the disarm: it resets the session to idle and abandons the
        # armed trial, while deliberately keeping the committed graph.
        greeted.session.hello(seed=greeted.seed)


@pytest.fixture
def two_state_graph(device):
    """A graph that holds one state for a fixed time, then ends.

    `wait` --500 ms--> `Hit`, raising one output line on entry. Small on purpose:
    it is not testing the engine, which the host suite covers exhaustively. It
    is the smallest thing that puts a real duration on a real clock and drives
    a real pin, which is the only part the host suite cannot reach.
    """
    graph = SingleGraphSetUploader(device.session, version=1)
    graph.begin(n_states=2, entry=0)
    graph.dist(0, kind="fixed", a=500)
    graph.state(0, terminal=None, timeout={"dist": 0, "target": 1})
    graph.action("entry", line=TRIAL_OUTPUT_LINE, kind="high")
    graph.state(1, terminal=1, timeout=None)
    ok = graph.end()
    assert ok[Field.MSG_TYPE] == MsgType.SET_OK, ok
    return graph


@pytest.fixture(scope="session")
def loopback(greeted) -> dict[int, int]:
    """The output-to-input jumpers, if somebody wired them.

    Probed rather than declared, and probed once. The alternative -- a flag
    saying "the harness is attached" -- is a flag that will one day be passed
    against a board where a wire has fallen out, and the tests it enables would
    then fail as if the *firmware* could not see its inputs.

    The probe is the smallest graph that can answer the question: raise every
    output line at once, read `io.in`, and compare against what the eight wires
    should have produced. Returns the map, or skips with the wiring list --
    naming the wires that are actually missing, since a harness with seven of
    eight wires in is the normal way this fails and "no loopback harness" would
    send somebody to check all of them.
    """
    device = greeted
    graph = SingleGraphSetUploader(device.session, version=99)
    graph.begin(n_states=2, entry=0)
    graph.dist(0, kind="fixed", a=250)
    graph.state(0, terminal=None, timeout={"dist": 0, "target": 1})
    for out_line in LOOPBACK:
        graph.action("entry", line=out_line, kind="high")
    graph.state(1, terminal=1, timeout=None)
    ok = graph.end()
    assert ok[Field.MSG_TYPE] == MsgType.SET_OK, ok

    device.request(MsgType.CONFIGURE, trial_id=9001, set_version=99, cap_ms=2000,
                   start="serial")
    device.request(MsgType.START, trial_id=9001)
    seen = device.state()["io"]["in"]
    device.settle(0.5)  # let the 250 ms state finish and its result arrive

    missing = [(o, i) for o, i in sorted(LOOPBACK.items()) if not seen & (1 << i)]
    if missing:
        board = (device.ack or {}).get("board")
        pins = LOOPBACK_PINS.get(board or "", [])
        named = ", ".join(
            f"{pins[o][0]}->{pins[o][1]}" if o < len(pins) else f"out {o}->in {i}"
            for o, i in missing
        )
        pytest.skip(
            f"the loopback harness is not wired: {len(missing)} of {len(LOOPBACK)} "
            f"wires missing ({named}).\n"
            "These tests drive the board's inputs from its own outputs. The harness "
            "is eight jumper wires, output line n to input line (n+4) mod 8:\n"
            + wiring_instructions(board)
        )
    return LOOPBACK


def pytest_report_header(config):
    return (
        "statemachined hardware suite, target "
        f"{config.getoption('--target') or DEFAULT_TARGET}"
    )
