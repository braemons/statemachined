# SPDX-License-Identifier: LGPL-3.0-or-later
"""Standing two processes up, and the arithmetic around them.

In a module of its own rather than in `conftest.py`, because pytest prepends
each test directory to `sys.path` and three suites with a `conftest.py` each
would give `import conftest` three meanings depending on which ran first.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bench_rig import BENCH_LINE_MAP, timed_graph  # noqa: F401  re-exported

#: How long to wait for each process to come up. Generous, because a first run
#: pays for an import of grpcio and uvicorn, and finite because a daemon that
#: never answers should fail this suite rather than hang it.
STARTUP_TIMEOUT_SECONDS = 30.0

def a_free_port() -> int:
    """A free port whose **successor is also free**, and hand both over.

    `statemachined serve` binds two: the panels on `--port` and gRPC on one
    above it. Asking the kernel for one port says nothing about the next, so a
    single probe made this suite fail intermittently with "Failed to bind to
    address 127.0.0.1:<n+1>" — a daemon that came up fine and then could not
    start its second listener.

    There is still a race between closing these sockets and the child binding
    them, and it is the one every test harness accepts: the alternative is a
    fixed port, and a fixed port makes two runs of this suite on one machine
    collide — which is a certainty rather than a race.
    """
    for _ in range(50):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            try:
                with socket.socket() as neighbour:
                    neighbour.bind(("127.0.0.1", port + 1))
            except OSError:
                continue
            return port
    raise AssertionError("no pair of consecutive free ports after 50 tries")


def the_command(*arguments: str) -> list[str]:
    """`statemachined ...`, from whichever environment is running these tests.

    The console script beside this interpreter rather than one on `$PATH`: the
    point of the suite is that *this* checkout's daemon works, and a stale one
    installed system-wide would answer perfectly well.
    """
    script = Path(sys.executable).parent / "statemachined"
    if not script.exists():
        pytest.skip(f"the statemachined console script is not beside {sys.executable}")
    return [str(script), *arguments]


class RunningProcess:
    """A child that is expected to stay up, and says so when it does not."""

    def __init__(self, command: list[str], name: str) -> None:
        self.name = name
        self.command = command
        self.process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

    def still_running(self) -> bool:
        return self.process.poll() is None

    def fail_if_it_died(self) -> None:
        if self.still_running():
            return
        # Whatever it printed on the way down is the only diagnosis there is,
        # and a bare "connection refused" from the next request would hide it.
        output = self.process.stdout.read() if self.process.stdout else ""
        raise AssertionError(
            f"{self.name} exited with {self.process.returncode}.\n"
            f"  command: {' '.join(self.command)}\n"
            f"  output:\n{output}"
        )

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def wait_until(predicate, timeout_seconds: float = 5.0):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None
