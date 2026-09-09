# SPDX-License-Identifier: LGPL-3.0-or-later
"""The whole thing, as a rig runs it: two processes, a socket, and no test doubles.

This suite exists because everything below it shares an address space with the
daemon, and three things are then never exercised: uvicorn, a real TCP
connection, and a real WebSocket handshake. Each has broken a shipped daemon at
least once in this family -- plain `uvicorn` answers a WebSocket upgrade with a
404, which is why `uvicorn[standard]` is a dependency and why the daemon's own
notes say it was found by connecting to a running one.

So:

    statemachined device   ← the firmware built for this machine, on a TCP port
            ▲ socket://127.0.0.1:<device port>
    statemachined serve    ← the shipped daemon, on an HTTP port
            ▲ http:// and ws://
    StatemachinedClient    ← with its own defaults: httpx and websockets

Both commands are the ones an operator with no board runs after `apt install`,
which is the second reason for the shape: what is under test is the artifact, up
to and including its command line.

**No board.** `statemachined_native_device` is the firmware's own session,
parser, validator, scan loop and result chunker compiled for the host. It is not
a mock and not a simulator of the protocol -- it is the protocol implementation.
What it cannot be is a *timing* test: its scan is a nanosleep on a preemptible
kernel. A board is `python/tests/hardware/`, with jumper wires.
"""

from __future__ import annotations

import json
import socket
import time

import pytest
from network_harness import (
    BENCH_LINE_MAP,
    STARTUP_TIMEOUT_SECONDS,
    RunningProcess,
    a_free_port,
    the_command,
)
from statemachined.device import native_device_on_a_socket
from statemachined.device.native_device_on_a_socket import the_native_device_is_built
from statemachined.client import StatemachinedClient, TransportError


@pytest.fixture(scope="module")
def device_port(tmp_path_factory) -> int:
    """`statemachined device`: the firmware, on this machine, on a TCP port.

    Module-scoped, because starting it costs a process and nothing a test does
    can leave it in a state the next test can see -- the daemon is restarted per
    test and greeting a board is what resets the session.
    """
    if not the_native_device_is_built():
        pytest.skip(native_device_on_a_socket.REASON_WHEN_NOT_BUILT)

    port = a_free_port()
    store = tmp_path_factory.mktemp("device") / "store.bin"
    device = RunningProcess(
        the_command("device", "--port", str(port)), name="statemachined device"
    )
    _wait_for_a_listener_on(port, device)
    try:
        yield port
    finally:
        device.stop()
        store.unlink(missing_ok=True)


@pytest.fixture
def rig(device_port, tmp_path):
    """`statemachined serve` in front of that device, and a client on a socket.

    A fresh daemon per test, with its own stores under the test's directory:
    graphs, configs, the trace and the recordings all persist, and a shared
    directory would make one test's saved graph the next test's store.

    The client is built with **no arguments but the URL**, which is the point of
    this suite: the httpx client and the websocket connection are the ones a
    caller gets by default.
    """
    configuration = tmp_path / "statemachined-rig-config.toml"
    config_directory = tmp_path / "configs"
    config_directory.mkdir()
    (config_directory / "bench.config.json").write_text(
        json.dumps({"name": "bench", "line_map": BENCH_LINE_MAP, "graphs": []}, indent=2)
    )
    configuration.write_text(
        "\n".join(
            [
                f'device_target = "socket://127.0.0.1:{device_port}"',
                "device_timeout_seconds = 5.0",
                f'graph_store_directory = "{tmp_path / "graphs"}"',
                f'state_machine_config_directory = "{config_directory}"',
                f'trace_directory = "{tmp_path / "trace"}"',
                f'recording_directory = "{tmp_path / "recordings"}"',
                'startup_state_machine_config = "bench"',
                "",
            ]
        )
    )

    port = a_free_port()
    daemon = RunningProcess(
        the_command(
            "serve",
            "--config",
            str(configuration),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-mdns",
        ),
        name="statemachined serve",
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        client = StatemachinedClient(base_url)
        _wait_until_it_answers(client, daemon)
        with client:
            yield client
    finally:
        daemon.stop()


def _wait_for_a_listener_on(port: int, process: RunningProcess) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        process.fail_if_it_died()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    process.stop()
    raise AssertionError(f"{process.name} never listened on {port}")


def _wait_until_it_answers(client: StatemachinedClient, process: RunningProcess) -> None:
    """Up, and holding the device. Both, because either alone is a false start.

    A daemon answering `/api/health` with `device_connected: false` is a daemon
    that has not finished greeting the board, and a test that armed a trial then
    would meet a 503 that says nothing about what it was testing.
    """
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    last = "it never answered"
    while time.monotonic() < deadline:
        process.fail_if_it_died()
        try:
            health = client.health()
        except TransportError as exc:
            last = str(exc)
        else:
            if health.get("device_connected"):
                return
            last = f"the daemon is up and has no device: {health}"
        time.sleep(0.1)
    process.stop()
    raise AssertionError(f"{process.name} never became ready: {last}")
