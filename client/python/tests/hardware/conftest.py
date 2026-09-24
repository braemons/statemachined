# SPDX-License-Identifier: LGPL-3.0-or-later
"""Fixtures for the suite that needs a board on the end of a cable.

Everything else in this repository is tested on the host, and those are real
tests of the logic. This directory is the part that cannot be: it runs against
silicon, through the daemon, the way a session does. `make test-hardware
TARGET=/dev/ttyACM0` runs it; `TARGET=native` runs it against the firmware
built for this machine, where everything but the timing budgets means the same.

Two consequences of "one real device" shape every file here:

* **Session scope.** One daemon, one greeting, for the whole run. Greeting a
  board takes the rig from it: one that was arming its own trials stops.
* **Tests must not leave a trial running.** A set cannot be committed while one
  is, so one careless test would fail the next four with an unrelated error.
  The `rig` fixture checks after every test and says which test did it.
"""

from __future__ import annotations

import shutil

import pytest
from bench_rig import (
    LOOPBACK,
    BenchRig,
    output,
    scratch_directory,
    start_daemon,
    start_native_device,
    wait_until,
    wiring_instructions,
)


@pytest.fixture(scope="session")
def bench(pytestconfig):
    target = pytestconfig.getoption("--target")
    if not target:
        pytest.skip("tests/hardware needs --target: a board, or `native`")
    scratch = scratch_directory()
    processes = []
    native = target == "native"
    try:
        if native:
            device, target = start_native_device(scratch)
            processes.append(device)
        daemon, client = start_daemon(target, scratch)
        processes.append(daemon)
        if not wait_until(lambda: client.read_device().connected, timeout_s=15):
            pytest.exit(
                f"the daemon could not reach a board at {target}\n"
                "  On Linux the reference board is usually /dev/ttyACM0. If it is missing,\n"
                "  double-tap reset to force the bootloader, and check group membership\n"
                "  (dialout/uucp). For a board on a network: make test-hardware TARGET=host:5000\n"
                f"  The daemon's log: {scratch / 'daemon.log'}",
                returncode=2,
            )
        lines = client.read_lines()
        yield BenchRig(
            client=client,
            board=client.read_device().board,
            native=native,
            board_input_pins=list(lines.board_input_pins),
            board_output_pins=list(lines.board_output_pins),
            scratch=scratch,
        )
        client.close()
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=10)
            except Exception:
                process.kill()
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture
def rig(bench, request):
    """The bench, and a check afterwards that the test left no trial behind."""
    yield bench
    state = bench.client.read_state()
    if state.running and state.trial_id is not None:
        bench.client.cancel_trial(state.trial_id)
        pytest.fail(f"{request.node.name} left trial {state.trial_id} running")


@pytest.fixture
def timed(rig):
    """The rig, for a test whose finding is a timing budget. See `within_budget`."""
    return rig


@pytest.fixture(scope="session")
def loopback(bench) -> dict[int, int]:
    """The eight wires, checked once: every output raised, every input read back."""
    everything = {
        "name": "loopback-check",
        "entry": "Raised",
        "distributions": {"hold": {"kind": "fixed", "duration_ms": 400}},
        "states": [
            {
                "name": "Raised",
                "on_entry": [{"line": output(line), "kind": "high"} for line in LOOPBACK],
                "timeout": {"after": "hold", "goto": "Done"},
            },
            {"name": "Done", "outcome": "HIT"},
        ],
    }
    bench.use(everything)
    trial_id = bench.next_trial_id()
    bench.client.configure_trial(trial_id, graph="loopback-check", cap_milliseconds=5000)
    bench.client.start_trial(trial_id)
    seen = 0
    wait_until(
        lambda: (bench.client.read_state().input_word or 0) & 0xFF == 0xFF, timeout_s=0.3
    )
    seen = bench.client.read_state().input_word or 0
    bench.client.wait_for_trial(trial_id, timeout_s=10)
    missing = [out for out, inp in LOOPBACK.items() if not seen & (1 << inp)]
    if missing:
        pytest.skip(
            f"the loopback harness is not wired: {len(missing)} of {len(LOOPBACK)} wires "
            f"carried nothing (input word {seen:#04x}). Wire it as:\n"
            + wiring_instructions(bench.board)
        )
    return LOOPBACK
