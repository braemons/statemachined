#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The firmware, on this machine, reachable at a TCP address.

`statemachined_native_device` is the firmware's own session and engine built for
the host (firmware/native/). It takes its link on stdin and stdout, and pyserial
opens a *URL*, so something has to sit between the two. This is that something:
a listening socket pumped to the child's pipes, so a daemon can be pointed at
`socket://127.0.0.1:5300` and talk to a real protocol implementation with no
board on the desk.

`socket://` is not a test-only contrivance -- it is how a daemon reaches an
ethernet-attached MCU, which is the case `device/serial_link.py` exists to make
indistinguishable from a cable. So the transport under the bench is the
transport the daemon ships.

**This is in the package, not beside it.** It was `daemon/bench/` until the
device became a shipped artifact, and the move is the point: a box that has
only ever seen `apt install braemons-statemachined` can now answer "does this
daemon work" without a board, which is the first question anybody asks of a rig
they have just installed. `statemachined device` runs it; the binary it starts
is installed at `libexec/statemachined-device` beside the vendored interpreter.

Three users, one implementation:

  * `statemachined device` -- the operator, on a fresh install;
  * `make bench-device` -- the same thing from a checkout, against `build/`;
  * `python/tests/integration/conftest.py`, which binds an ephemeral port and
    drops the link on purpose.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

#: An explicit override, which is what a checkout with an unusual build
#: directory has and what a test harness reaches for. Checked first because a
#: person who has said where the binary is has already answered the question.
BINARY_ENVIRONMENT_VARIABLE = "STATEMACHINED_NATIVE_DEVICE"

#: The name the package installs, both under the vendored prefix and on PATH.
INSTALLED_BINARY_NAME = "statemachined-device"


def _candidate_paths() -> list[Path]:
    """Everywhere the device could be, nearest first.

    The order is not a preference so much as a description of who is asking.
    An installed daemon runs from a vendored interpreter and the binary is its
    sibling; a checkout has `build/` and no installation at all; and a
    developer who has built somewhere else says so in the environment. Each
    case finds its own answer without knowing the others exist.
    """
    override = os.environ.get(BINARY_ENVIRONMENT_VARIABLE)
    if override:
        return [Path(override)]

    candidates = []

    # The installed tree: /opt/braemons/statemachined/{bin/python3,libexec/...}.
    # Derived from the running interpreter rather than hard-coded, so a tree
    # relocated or unpacked somewhere else still finds its own device.
    interpreter_prefix = Path(sys.executable).resolve().parent.parent
    candidates.append(interpreter_prefix / "libexec" / INSTALLED_BINARY_NAME)

    # A checkout: this file is python/src/statemachined/device/..., so the
    # repository root is four levels up, and `make integration-device` writes
    # into build/ there.
    repository_root = Path(__file__).resolve().parents[4]
    candidates.append(repository_root / "build" / "statemachined_native_device")

    on_path = shutil.which(INSTALLED_BINARY_NAME)
    if on_path:
        candidates.append(Path(on_path))

    return candidates


def the_native_device_binary() -> Path | None:
    """The first candidate that exists and can be run, or None."""
    for candidate in _candidate_paths():
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    return None


def the_native_device_is_built() -> bool:
    return the_native_device_binary() is not None


def _reason_when_not_built() -> str:
    looked_in = "\n".join(f"    {candidate}" for candidate in _candidate_paths())
    return (
        "The native device binary is not here. Looked in:\n"
        f"{looked_in}\n"
        "  From a checkout, build it with `make integration-device` "
        "(`make test` and `make test-integration` both do).\n"
        f"  From a package, it ships at <prefix>/libexec/{INSTALLED_BINARY_NAME}; "
        "a package without it was built without a compiler.\n"
        f"  Or name it in ${BINARY_ENVIRONMENT_VARIABLE}."
    )


#: Kept as a module-level name because two test modules import it. It is
#: computed lazily through __getattr__ below rather than at import time: the
#: message names the paths that were searched, and on an installed daemon that
#: search touches the filesystem for a string almost nobody reads.
__all__ = [
    "DEFAULT_BENCH_PORT",
    "NativeDeviceOnASocket",
    "REASON_WHEN_NOT_BUILT",
    "the_native_device_binary",
    "the_native_device_is_built",
]


def __getattr__(name: str) -> object:
    if name == "REASON_WHEN_NOT_BUILT":
        return _reason_when_not_built()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


DEFAULT_BENCH_PORT = 5300


class NativeDeviceOnASocket:
    """One child process, reachable at a TCP address.

    The bridge is two threads because the child's stdin and stdout are separate
    pipes and either may block: a single loop would deadlock the first time the
    device wrote a result while the caller was still sending an upload.
    """

    def __init__(
        self,
        port: int = 0,
        store_path: str | None = None,
        loopback: str | None = None,
    ) -> None:
        """`port` 0 asks the kernel for a free one, which is what a test wants;
        a fixed one is what a bench wants, so the URL can be written down.

        `store_path` is where this device keeps the settings it remembers across
        a restart -- its data flash, in effect. Given one, two devices started
        with the same path are the same board before and after a power cut,
        which is how the stored-settings behaviour is tested at all. Left out,
        the device uses its own default in the working directory.

        `loopback` wires the device's outputs back to its inputs in software --
        `"8"` for output line n on input line (n + 4) mod 8, which is the
        loopback harness of dev/HARDWARE.md with no jumper wires in it, or
        `"<width>:<shift>"` to say both. It is what lets a suite that drives
        transitions from *predicates* run unchanged with a board and without
        one. Left out, the device has no inputs at all, which is what every
        other test in this tree expects of it.
        """
        self._listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listening_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listening_socket.bind(("127.0.0.1", port))
        self._listening_socket.listen(1)
        self.port = self._listening_socket.getsockname()[1]

        self._store_path = store_path
        self._loopback = loopback
        self._process: subprocess.Popen | None = None
        self._connection: socket.socket | None = None
        self._stop = threading.Event()
        self.report = lambda message: None

    @property
    def target_url(self) -> str:
        return f"socket://127.0.0.1:{self.port}"

    def start(self) -> None:
        binary = the_native_device_binary()
        if binary is None:
            raise FileNotFoundError(_reason_when_not_built())
        overrides = {}
        if self._store_path is not None:
            overrides["STATEMACHINED_STORE"] = self._store_path
        if self._loopback is not None:
            overrides["STATEMACHINED_LOOPBACK"] = self._loopback
        environment = {**os.environ, **overrides} if overrides else None
        self._process = subprocess.Popen(
            [str(binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env=environment,
        )
        # One permanent reader of the device's output, and one accept loop. The
        # reader cannot be per-connection: it would be parked in a blocking
        # read() when the link dropped, and nothing could join it.
        threading.Thread(target=self._pump_device_into_whatever_is_connected, daemon=True).start()
        threading.Thread(target=self._accept_connections_forever, daemon=True).start()

    def _accept_connections_forever(self) -> None:
        """One connection at a time, and then the next one.

        A loop rather than a single accept, because a reconnect is one of the
        things worth exercising: the daemon closes the port, opens it again, and
        the device it comes back to is the same process with the same committed
        set.
        """
        while not self._stop.is_set():
            try:
                connection, _ = self._listening_socket.accept()
            except OSError:
                return
            self._connection = connection
            self.report("a daemon connected")
            # Returns as soon as this connection closes, which is what a link
            # loss looks like from the device's side.
            self._pump_socket_into_device(connection)
            self._connection = None
            self.report("the daemon disconnected; the device is still running")

    def _pump_socket_into_device(self, connection: socket.socket) -> None:
        # Bound once, rather than read from `self` on every chunk: `stop()`
        # clears `_process` while this thread may still be inside a `recv`, and
        # a pump that then reached for `self._process.stdin` would raise on a
        # shutdown that is entirely ordinary.
        process = self._process
        assert process is not None and process.stdin is not None
        while not self._stop.is_set():
            try:
                chunk = connection.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            try:
                process.stdin.write(chunk)
                process.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                return

    def _pump_device_into_whatever_is_connected(self) -> None:
        """Everything the device says goes to the open link, or nowhere.

        Nowhere is a real case and not an error: a device whose host has gone
        away carries on scanning and carries on talking, and what it emits into
        a closed link is lost. That is what link loss *is*, and it is why a
        reconnecting daemon drops whatever was buffered before it greets.
        """
        process = self._process
        assert process is not None and process.stdout is not None
        while not self._stop.is_set():
            try:
                chunk = process.stdout.read(1)
            except ValueError:
                # The pipe was closed under us by `stop()`. Same shutdown, same
                # answer: this thread's job is over.
                return
            if not chunk:
                return
            connection = self._connection
            if connection is None:
                continue
            try:
                connection.sendall(chunk)
            except OSError:
                continue

    def drop_the_link(self) -> None:
        """Close the socket under the daemon, the way a USB port closing does.

        The device keeps running, which is exactly the case that matters: its
        committed set is still there and a reconnect must not cost a re-upload.
        """
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def stop(self) -> None:
        self._stop.set()
        self.drop_the_link()
        try:
            self._listening_socket.close()
        except OSError:
            pass
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None


def main(argv: list[str]) -> int:
    if not the_native_device_is_built():
        print(_reason_when_not_built(), file=sys.stderr)
        return 1

    port = int(argv[1]) if len(argv) > 1 else DEFAULT_BENCH_PORT
    device = NativeDeviceOnASocket(port)
    device.report = lambda message: print(message, flush=True)
    device.start()
    print(f"native device on {device.target_url} (ctrl-c to stop)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        device.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
