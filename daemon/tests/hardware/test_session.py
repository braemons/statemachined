# SPDX-License-Identifier: GPL-3.0-or-later
"""The greeting, and what a board says about itself.

dev/BRINGUP.md §4 done by machine: the numbers a person would read off the
`hello_ack` and squint at, asserted instead.
"""

from __future__ import annotations

import time

import pytest
from conftest import SCAN_HZ_TARGET
from statemachined.device.messages import ErrorCode, Field, MsgType

PROTO = 1


def test_refuses_everything_before_hello(pre_hello_probe):
    """A command sent before a session exists is refused, and says so.

    The one guarantee in this file that cannot be re-asked: after the greeting
    the device is out of `Greeting` for good, so this reads a probe taken
    before it. A board greeted by an earlier run skips rather than fails --
    it is not evidence either way, and demanding a reset before every run would
    make the suite something nobody runs.
    """
    if pre_hello_probe is None:
        pytest.skip("this board was already greeted since it was last reset")
    assert pre_hello_probe["code"] == ErrorCode.NOT_READY
    # Every refusal names what to change, and for this one the answer is the
    # command the host forgot to send.
    assert pre_hello_probe["context"] == "hello"


def test_hello_ack_identifies_the_board(greeted):
    ack = greeted.ack
    assert ack[Field.MSG_TYPE] == MsgType.HELLO_ACK
    assert ack["proto"] == PROTO
    assert ack["board"], "a board that will not name itself cannot be matched to a pinout"
    assert ack["fw"], "the firmware version is how a board in a rack is identified"
    assert ack["n_input_lines"] > 0 and ack["n_output_lines"] > 0


def test_hello_ack_declares_the_limits_a_host_must_respect(greeted):
    """`caps` is a contract, not a status line.

    A bridge sizes its uploads from these. A missing member would be read as
    "no limit" by anything doing arithmetic on it, so their presence is the
    assertion; their values are the board's business.
    """
    caps = greeted.ack["caps"]
    for key in (
        "max_line",
        "max_states",
        "max_transitions",
        "max_output_actions",
        "max_distributions",
        "max_choice_options",
        "max_path",
    ):
        assert isinstance(caps.get(key), int) and caps[key] > 0, f"caps.{key}"


def test_scan_hz_meets_the_target(greeted):
    """The number M3 is waiting on.

    Measured at boot rather than declared -- 2000 timed repetitions of reading
    and conditioning the pins -- so it is what the board achieves, not what the
    design hoped for. It is a floor: evaluating a state's transitions sits on
    top of it. Under Renode this would be meaningless, which is exactly why
    this assertion lives in the suite that needs real silicon.
    """
    hz = greeted.ack["scan_hz"]
    assert isinstance(hz, int)
    assert hz >= SCAN_HZ_TARGET, (
        f"scan_hz {hz} is below the {SCAN_HZ_TARGET} Hz target (dev/PLAN.md M3). "
        "This is a finding about the board, not about the test."
    )


def test_hello_is_refused_for_a_protocol_version_the_board_does_not_speak(device):
    """Better a refusal than a host and a device disagreeing about the wire."""
    error = device.refuse(MsgType.HELLO, proto=PROTO + 100, seed="ABCDEF0123456789")
    assert error.code == ErrorCode.BAD_PROTO
    assert error.context == "proto"


def test_seed_must_be_hex_not_a_number(device):
    """64 bits do not survive a double.

    A seed sent as a JSON number would be silently rounded by anything using
    IEEE doubles, and a trial that cannot be replayed is a reproducibility bug
    nobody would find later. So the device refuses the number rather than
    accepting an approximation of it.
    """
    error = device.refuse(MsgType.HELLO, proto=PROTO, seed=0x443ADD5C803378B8)
    assert error.code == ErrorCode.BAD_JSON
    assert error.context == "seed"


def test_ping_answers_and_its_clock_advances(device):
    first = device.request(MsgType.PING)
    assert first[Field.MSG_TYPE] == MsgType.PONG
    time.sleep(0.05)
    second = device.request(MsgType.PING)
    assert second["up_us"] > first["up_us"], "the device's clock is not moving"


def test_state_report_carries_what_bringup_reads_off_it(device):
    """§5's reply, member by member.

    `io` is the only way anything outside the device can check that a graph's
    line numbers reach the pins somebody wired -- there is no read-back path --
    and `scan` is where an overrun becomes visible instead of being absorbed.
    """
    report = device.state()
    assert report[Field.MSG_TYPE] == MsgType.STATE_REPORT

    io = report["io"]
    assert isinstance(io["in"], int) and isinstance(io["out"], int)

    scan = report["scan"]
    for key in ("hz", "overruns", "worst_gap", "tx_stalls"):
        assert isinstance(scan.get(key), int), f"scan.{key}"

    for key in ("link_state", "graph", "running", "up_us"):
        assert key in report, key
    assert isinstance(report["dropped_lines"], int)
    assert isinstance(report["bad_lines"], int)


def test_the_reported_scan_rate_agrees_with_the_greeting(device, greeted):
    """Two paths to the same number, which have drifted apart before."""
    assert device.state()["scan"]["hz"] == greeted.ack["scan_hz"]
