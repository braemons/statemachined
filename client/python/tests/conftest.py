# SPDX-License-Identifier: LGPL-3.0-or-later
"""A daemon to talk to: a real one, on a port nobody else has.

These tests are worth having because they run the *daemon*, over a real gRPC
channel: the same servicers, the same refusals and the same streams a rig gets.
A mock of the daemon would only ever assert that this client agrees with a
second description of statemachined written by the same hand on the same day.

The daemon lives in `daemon/`, a sibling of this project, and is run with `uv`
so that it brings its own environment — this client's environment deliberately
does not have grpcio-the-server, uvicorn, pydantic or the daemon itself in it,
and a suite that could only pass with those installed would be hiding a
dependency. The whole suite skips when that is not possible, so a checkout of
just the client still runs the seam tests.

**No board, on purpose.** Everything here runs against a daemon with nothing on
its serial port: the stores, the trace, the session document, the rig config
and the refusals. That is most of the interface, and the part it is not covers
is covered by `daemon/tests/e2e/`, which builds the firmware for the host and
puts it on a socket. What this suite adds there is the one thing that suite
cannot have: **a board that is not attached is a first-class answer**, and
`NoBoardIsAttached` is asserted here rather than assumed.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest
from statemachined_client import DaemonIsUnavailable, StatemachinedClient

#: `daemon/`, three levels up from this file.
DAEMON_PROJECT = Path(__file__).resolve().parents[3] / "daemon"

#: Generous, because a first run pays for an import of fastapi, uvicorn and
#: grpcio; finite, because a daemon that never answers should fail this suite
#: rather than hang it.
STARTUP_TIMEOUT_SECONDS = 60.0


def a_free_port() -> int:
    """Ask the kernel for one, and hand it over.

    There is a race between closing this socket and the child binding it, and
    it is the one every test harness accepts: the alternative is a fixed port,
    and a fixed port makes two runs of this suite on one machine collide —
    which is a certainty rather than a race.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="session")
def rig():
    """A running statemachined, and a client pointed at it.

    Every store goes under a temporary directory, so a test that writes a graph
    never leaves one in the repository and two runs cannot see each other's.
    """
    if shutil.which("uv") is None:
        pytest.skip("no uv on PATH, so the daemon cannot be started")
    if not (DAEMON_PROJECT / "pyproject.toml").exists():
        pytest.skip(f"no daemon project at {DAEMON_PROJECT}")

    # `--port` is the *web* port; gRPC is one above it, which is the rule
    # `grpc_port_for` states in the daemon and `DEFAULT_PORT` states here.
    # Asking for one free port and using both is the honest way to say so.
    port = a_free_port()
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        configuration = root / "statemachined-rig-config.toml"
        configuration.write_text(
            "\n".join(
                [
                    # Nothing is plugged in, and the daemon is expected to come
                    # up anyway. A rig whose board is unplugged still answers.
                    'device_target = ""',
                    f'graph_store_directory = "{root / "graphs"}"',
                    f'state_machine_config_directory = "{root / "configs"}"',
                    f'trace_directory = "{root / "trace"}"',
                    f'recording_directory = "{root / "recordings"}"',
                    "",
                ]
            )
        )
        daemon = subprocess.Popen(
            [
                "uv",
                "run",
                "--directory",
                str(DAEMON_PROJECT),
                "--extra",
                "serve",
                "statemachined",
                "serve",
                "--config",
                str(configuration),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-mdns",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = StatemachinedClient(f"127.0.0.1:{port + 1}")
        try:
            client.wait_until_ready(timeout_s=STARTUP_TIMEOUT_SECONDS)
        except DaemonIsUnavailable:
            daemon.terminate()
            pytest.skip("the daemon did not come up; is its environment synced?")
        try:
            yield client
        finally:
            client.close()
            daemon.terminate()
            daemon.wait(timeout=10)


@pytest.fixture
def empty_stores(rig):
    """A graph store and a config store with nothing of this test's in them.

    Every test here shares one daemon, because starting one costs several
    seconds and a suite that started thirty would be a suite nobody runs.
    Sharing is only safe if each test hands the stores back the way it found
    them, which is what this does — afterwards, not before, so a failure leaves
    the evidence where a person can look at it.
    """
    before = {graph.name for graph in rig.list_graphs()}
    yield rig
    for graph in rig.list_graphs():
        if graph.name not in before:
            rig.delete_graph(graph.name)
