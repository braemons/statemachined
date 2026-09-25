# SPDX-License-Identifier: LGPL-3.0-or-later
"""A daemon to talk to: a real one, on a port nobody else has.

These tests are worth having because they run the *daemon*, over a real gRPC
channel: the same servicers, the same refusals and the same streams a rig gets.
A mock of the daemon would only ever assert that this client agrees with a
second description of statemachined written by the same hand on the same day.

The daemon is the Rust binary this repository builds — `cargo build` puts it at
`target/debug/statemachined` — or whichever one `$STATEMACHINED_BINARY` names.
The whole suite skips when there is none, so a checkout of just the client
still runs the seam tests.

**No board, on purpose.** Everything here runs against a daemon with nothing on
its serial port: the stores, the trace, the session document, the rig config
and the refusals. That is most of the interface, and the part it is not covers
is covered by the family's e2e suite in `contracts/e2e-tests/`, which puts the
firmware built for the host on a socket. What this suite adds there is the one thing that suite
cannot have: **a board that is not attached is a first-class answer**, and
`NoBoardIsAttached` is asserted here rather than assumed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest
from statemachined_client import DaemonIsUnavailable, StatemachinedClient

#: The daemon a checkout builds: the workspace's `target/`, three levels up.
BUILT_DAEMON = Path(__file__).resolve().parents[3] / "target" / "debug" / "statemachined"

#: Finite, because a daemon that never answers should fail this suite rather
#: than hang it.
STARTUP_TIMEOUT_SECONDS = 30.0


def the_daemon_binary() -> Path | None:
    named = os.environ.get("STATEMACHINED_BINARY")
    candidate = Path(named) if named else BUILT_DAEMON
    return candidate if candidate.is_file() else None


def a_free_port() -> int:
    """A free port, for a daemon of this suite's own.

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
    binary = the_daemon_binary()
    if binary is None:
        pytest.skip(f"no daemon at {BUILT_DAEMON}; `cargo build` makes one")

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
                str(binary),
                "serve",
                "--rig-config",
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
        client = StatemachinedClient(f"127.0.0.1:{port}")
        try:
            client.wait_until_ready(timeout_s=STARTUP_TIMEOUT_SECONDS)
        except DaemonIsUnavailable:
            daemon.terminate()
            pytest.fail(f"{binary} did not come up on 127.0.0.1:{port}")
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


def pytest_addoption(parser):
    # Here, in the top-level conftest, because pytest only takes options from
    # the conftests it loads before collection. `tests/hardware/` reads it.
    parser.addoption(
        "--target",
        default=None,
        help="the board for tests/hardware: a device path, a host:port, or `native` for "
        "the firmware built for this machine. Without it tests/hardware is skipped.",
    )
