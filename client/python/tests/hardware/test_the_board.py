# SPDX-License-Identifier: LGPL-3.0-or-later
"""What the board says about itself, and whether it is true.

`hello_ack` is where a board declares its identity, its limits and its scan
rate; the daemon reports them as `DeviceState`. The scan rate is the one worth
checking against silicon, because it is measured at boot rather than declared,
and a board that cannot keep up says so here first.
"""

from __future__ import annotations

from bench_rig import SCAN_HZ_TARGET, within_budget


def test_the_board_identifies_itself(rig):
    device = rig.client.read_device()
    assert device.connected
    assert device.board, "the board did not say what it is"
    assert device.firmware_version, "the board did not say what it runs"
    assert device.protocol_version == 2, (
        f"protocol {device.protocol_version}: the daemon and this board disagree about the link"
    )


def test_the_board_declares_the_limits_a_host_must_respect(rig):
    capacities = rig.client.read_device().capacities
    assert capacities is not None
    # The frame budget the link was designed around, and pools a graph can use.
    assert capacities.max_line >= 512
    for name in (
        "max_states",
        "max_transitions",
        "max_output_actions",
        "max_distributions",
        "max_path",
        "max_graphs",
        "max_timers",
    ):
        assert getattr(capacities, name) > 0, f"{name} is {getattr(capacities, name)}"
    assert capacities.input_line_count >= 8 and capacities.output_line_count >= 8, (
        "the loopback harness needs eight lines each way"
    )


def test_the_board_names_its_pins(rig):
    """What `pins` answers: the labels a line map is written against."""
    assert len(rig.board_input_pins) >= 8
    assert len(rig.board_output_pins) >= 8
    assert all(rig.board_input_pins) and all(rig.board_output_pins)


def test_scan_hz_meets_the_target(timed):
    """Measured at boot, not declared: the timing resolution this board delivers."""
    hz = timed.client.read_device().measured_scan_hz
    within_budget(
        timed,
        hz >= SCAN_HZ_TARGET,
        (f"the board scans at {hz} Hz, below the {SCAN_HZ_TARGET} Hz target"),
    )


def test_the_state_report_carries_what_bringup_reads_off_it(rig):
    state = rig.client.read_state()
    assert state.connected
    assert state.input_word is not None and state.output_word is not None
    assert state.scan is not None and state.scan.hz > 0


def test_the_reported_scan_rate_agrees_with_the_greeting(rig):
    assert rig.client.read_state().scan.hz == rig.client.read_device().measured_scan_hz
