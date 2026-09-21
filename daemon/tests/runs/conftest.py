# SPDX-License-Identifier: GPL-3.0-or-later
"""The same runs, four ways: two API paths, two far ends.

Everything else in `daemon/tests/` fixes one of those two axes. `integration/`
and `e2e/` always talk to the firmware built for this machine; `hardware/`
always talks to a board, and does it with `RequestResponseSession` because that
is what a bench instrument uses. So the question this tier exists to answer has
never been asked anywhere: **does a session that works against the host device
work against a board, and does it work the same through both ways of driving
one?**

    far end   ─┬─ the firmware built for this machine, on a socket   (default)
               └─ an Arduino Uno R4 Minima on a cable   (--target=/dev/ttyACM0)

    API path  ─┬─ StatemachinedClient -> the daemon's gRPC API -> the device
               └─ StatemachinedDevice, which opens the port itself

Every test below runs in all four cells, and the paradigms are one definition
shared between them (`paradigms.py`). A run that passes here has passed against
real silicon and against a host build, driven by the two things the package
offers for driving it -- which is the claim `dev/PLAN.md` M6 needs and nothing
in this tree could previously make.

**Why the far end can be swapped at all.** Because the board presses its own
levers. Output line n is wired to input line (n + 4) mod 8 -- eight jumper wires
on a board, `set_native_loopback()` in the host build -- so a paradigm's own
entry action supplies the stimulus its transitions wait for. Without that, half
of these paradigms could only ever run on hardware and CI would test the other
half twice.

**Why the daemon is in-process here.** `tests/e2e/` already runs `statemachined
serve` as a subprocess and is the only place the shipped command line and its
two listeners are exercised; repeating that here would double the runtime to
re-prove it. What this tier varies is the far end. The daemon here is the
shipped servicers over a `grpc.aio` server on a loopback port
(`tests/grpc_harness.py`), so every call below is the real rpc, the real
serialisation, the real refusal and the real device underneath.

**One board, one holder.** A serial port admits one opener, so the two API
paths must never be up at once. Both fixtures are function-scoped and close what
they opened, which is what keeps `--target=/dev/ttyACM0` from failing every
second test with `device or resource busy`.
"""

from __future__ import annotations

import json
import time

import pytest
from grpc_harness import DaemonOnALoopbackPort
from paradigms import LOOPBACK_LINE_MAP
from statemachined_client import DaemonRefusedTheRequest, StatemachinedClient

from statemachined.daemon.rig_configuration import RigConfiguration
from statemachined.device import native_device_on_a_socket
from statemachined.device.native_device_on_a_socket import (
    NativeDeviceOnASocket,
    the_native_device_is_built,
)
from statemachined.device.statemachined_device import StatemachinedDevice
from statemachined.model.line_map import LineMap

#: The loopback harness as the host build takes it: eight lines, shifted by four.
#: The same rule the jumper wires implement, so `paradigms.DRIVES` is true of
#: both far ends. See `hal::set_native_loopback`.
SOFTWARE_HARNESS = "8"

# `--target` is registered in daemon/tests/conftest.py rather than here, because
# `hardware/` takes it as well and pytest registers each option once per run.
# Unset is falsy, and every reader below treats that as "the host build".


def pytest_report_header(config):
    target = config.getoption("--target")
    return "statemachined runs, far end: " + (target or "the host build, with a software harness")


@pytest.fixture(scope="session")
def a_board_is_attached(pytestconfig) -> bool:
    return bool(pytestconfig.getoption("--target"))


@pytest.fixture
def far_end(pytestconfig, tmp_path):
    """Whatever these runs are driving, as a target URL.

    Function-scoped even for the host build, which costs a process per test and
    buys the thing that matters most in a tier like this: a device that cannot
    carry one test's committed set, armed trial or stored settings into the
    next. A board cannot be replaced between tests, so for `--target` the
    fixture hands over the same one and the greeting is what resets it.
    """
    target = pytestconfig.getoption("--target")
    if target:
        yield target
        return

    if not the_native_device_is_built():
        pytest.skip(native_device_on_a_socket.REASON_WHEN_NOT_BUILT)
    device = NativeDeviceOnASocket(
        store_path=str(tmp_path / "store.bin"), loopback=SOFTWARE_HARNESS
    )
    device.start()
    try:
        yield device.target_url
    finally:
        device.stop()


# ------------------------------------------------------- path 1: the daemon ---


@pytest.fixture
def rig(far_end, tmp_path):
    """`StatemachinedClient` -> the shipped daemon -> whatever `far_end` is.

    Stores under the test's own directory, so one test's saved graph is not the
    next test's store. The line map is pushed at startup from the state-machine
    config, which is how a rig does it -- a test that pushed wiring by hand
    would be testing a call no operator makes.
    """
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "runs.config.json").write_text(
        json.dumps({"name": "runs", "line_map": LOOPBACK_LINE_MAP, "graphs": []}, indent=2)
    )
    configuration = RigConfiguration(
        device_target=far_end,
        device_timeout_seconds=10.0,
        graph_store_directory=tmp_path / "graphs",
        state_machine_config_directory=configs,
        trace_directory=tmp_path / "trace",
        recording_directory=tmp_path / "recordings",
        heartbeat_seconds=0.5,
        startup_state_machine_config="runs",
    )
    daemon = DaemonOnALoopbackPort(configuration)
    daemon.start()
    try:
        with daemon.client() as client:
            client.wait_until_ready(timeout_s=STARTUP_TIMEOUT_SECONDS)
            _wait_until_it_holds_the_device(client)
            yield client
    finally:
        daemon.stop()


def _wait_until_it_holds_the_device(client: StatemachinedClient) -> None:
    """Up *and* greeted, because either alone is a false start.

    The harness returns once the server is listening and the link thread has
    been started -- greeting the far end is a round trip that happens after it.
    A test that armed a trial in that window gets its result read back across
    the daemon's first connection rather than after it, and the daemon answers
    `no_result_yet` about a trial that plainly ran. That is a race, not a
    flake: it is lost more often against a board, where the greeting is a real
    serial round trip rather than a socket on loopback.
    """
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    last: object = "it never answered"
    while time.monotonic() < deadline:
        try:
            health = client.read_health()
        except DaemonRefusedTheRequest as refused:
            last = str(refused)
        else:
            if health.device_connected:
                return
            last = f"the daemon is up and has no device: {health}"
        time.sleep(0.05)
    raise AssertionError(f"the daemon never took hold of the device: {last}")


# ------------------------------------------------------- path 2: the device ---


@pytest.fixture
def device(far_end):
    """`StatemachinedDevice`, holding the port itself. No daemon anywhere.

    The other half of the package, and a different claim: this is what a script
    that owns the rig uses, and it compiles, uploads and names results without
    a store, a trace or an HTTP layer being involved at all.
    """
    supervisor = StatemachinedDevice(
        far_end, LineMap.model_validate(LOOPBACK_LINE_MAP), timeout=10.0
    )
    supervisor.connect_and_greet()
    supervisor.push_wiring()
    try:
        yield supervisor
    finally:
        supervisor.disconnect()


# --------------------------------------------------------------- the harness ---


#: How long to wait for a daemon to come up and greet. Generous because a first
#: run pays for imports; finite because a daemon that never greets should fail
#: this suite rather than hang it.
STARTUP_TIMEOUT_SECONDS = 30.0


def _harness_probe_document() -> dict:
    """The smallest graph that can answer "are the outputs reaching the inputs".

    Raise every driven output on entry and put a transition on every driven
    input. A run that leaves the first state by `transition` saw its own outputs
    come back; one that leaves by `timeout` did not.
    """
    from paradigms import DRIVES

    return {
        "name": "harness-probe",
        "entry": "Raise",
        "distributions": {"settle": {"kind": "fixed", "duration_ms": 60}},
        "states": [
            {
                "name": "Raise",
                "on_entry": [{"line": line, "kind": "high"} for line in DRIVES],
                "timeout": {"after": "settle", "goto": "Seen"},
                "transitions": [{"when": {"all": list(DRIVES.values())}, "goto": "Seen"}],
            },
            {"name": "Seen", "outcome": "HIT"},
        ],
    }


def _the_harness_answer(wired: bool, a_board_is_attached: bool):
    """One verdict, however it was reached, so both paths say the same thing.

    Probed rather than assumed even for the host build, where this file switches
    it on two fixtures up: the probe costs one 60 ms trial and it is the whole
    difference between "these tests were skipped" and "these tests silently
    stopped testing anything" if that flag is ever dropped.
    """
    from paradigms import DRIVES

    if wired:
        return DRIVES
    if a_board_is_attached:
        pytest.skip(
            "the loopback harness is not wired. These runs drive transitions from the "
            "board's own outputs; without the jumpers half of them could only ever time "
            "out. Eight wires, output line n to input line (n+4) mod 8 -- D10->D6, "
            "D11->D7, D12->D8, A0->D9, A1->D2, A2->D3, A3->D4, A4->D5. "
            "See docs/operations/hardware.md."
        )
    raise AssertionError(
        "the host build's software harness is not on. It is switched on with "
        f"STATEMACHINED_LOOPBACK={SOFTWARE_HARNESS} by the `far_end` fixture in this "
        "file; without it every paradigm that waits on a line would time out, and "
        "these tests would pass against nothing."
    )


@pytest.fixture
def the_loopback_harness(device, a_board_is_attached):
    """Whether outputs reach inputs, asked down the direct path.

    There are two of these fixtures rather than one, and the reason is the
    serial port: **a board admits one opener**. A single probe fixture would
    have to hold a connection of its own, and a daemon test using it would then
    have the daemon and the probe both wanting the cable. Each path therefore
    asks the question through the connection it already has.
    """
    from statemachined.model.graph_definition import GraphDefinition

    probe = _harness_probe_document()
    device.upload_graph_set([GraphDefinition.model_validate(probe)], set_version=1)
    result = device.run_trial_to_completion(9001, "harness-probe", cap_milliseconds=3000)
    return _the_harness_answer(result.visits[0].exit_cause == "transition", a_board_is_attached)


@pytest.fixture
def the_loopback_harness_over_the_api(rig, a_board_is_attached):
    """The same question, asked down the daemon path. See above for why two."""
    probe = _harness_probe_document()
    rig.write_graph(probe["name"], json.dumps(probe))
    rig.upload_graph_set(["harness-probe"])
    rig.configure_trial(9001, graph="harness-probe", cap_milliseconds=3000)
    rig.start_trial(9001)
    # `wait_for_trial` carries the ring's backlog, so a 60 ms trial that ended
    # before this call is still seen. The deadline is the caller's: only this
    # side knows a trial is in flight.
    rig.wait_for_trial(9001, timeout_s=10)
    result = rig.read_trial_result()
    return _the_harness_answer(
        result.visits[0].exit_cause == "transition", a_board_is_attached
    )
