#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The firmware, on this machine, reachable at a TCP address.

`build/statemachined_native_device` is the firmware's own session and engine
built for the host (firmware/native/). It takes its link on stdin and stdout,
and pyserial opens a *URL*, so something has to sit between the two. This is
that something: a listening socket pumped to the child's pipes, so a daemon can
be pointed at `socket://127.0.0.1:5300` and talk to a real protocol
implementation with no board on the desk.

`socket://` is not a test-only contrivance -- it is how a daemon reaches an
ethernet-attached MCU, which is the case `device/serial_link.py` exists to make
indistinguishable from a cable. So the transport under the bench is the
transport the daemon ships.

Two users, one implementation:

  * `daemon/tests/integration/conftest.py` imports `NativeDeviceOnASocket`,
    binds it to an ephemeral port, and drops the link on purpose;
  * `make bench-device` runs this file, which binds a fixed port and waits, so
    that `make bench TARGET=socket://127.0.0.1:5300` finds it.

    python3 daemon/bench/native_device_on_a_socket.py [port]
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NATIVE_DEVICE_BINARY = REPOSITORY_ROOT / "build" / "statemachined_native_device"

REASON_WHEN_NOT_BUILT = (
    f"{NATIVE_DEVICE_BINARY} is not built. Run `make test` or `make integration-device` first; "
    "`make test-integration` does both."
)

DEFAULT_BENCH_PORT = 5300


def the_native_device_is_built() -> bool:
    return NATIVE_DEVICE_BINARY.exists() and os.access(NATIVE_DEVICE_BINARY, os.X_OK)


class NativeDeviceOnASocket:
    """One child process, reachable at a TCP address.

    The bridge is two threads because the child's stdin and stdout are separate
    pipes and either may block: a single loop would deadlock the first time the
    device wrote a result while the caller was still sending an upload.
    """

    def __init__(self, port: int = 0) -> None:
        """`port` 0 asks the kernel for a free one, which is what a test wants;
        a fixed one is what a bench wants, so the URL can be written down."""
        self._listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listening_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listening_socket.bind(("127.0.0.1", port))
        self._listening_socket.listen(1)
        self.port = self._listening_socket.getsockname()[1]

        self._process: subprocess.Popen | None = None
        self._connection: socket.socket | None = None
        self._stop = threading.Event()
        self.report = lambda message: None

    @property
    def target_url(self) -> str:
        return f"socket://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._process = subprocess.Popen(
            [str(NATIVE_DEVICE_BINARY)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
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
        assert self._process is not None and self._process.stdin is not None
        while not self._stop.is_set():
            try:
                chunk = connection.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            try:
                self._process.stdin.write(chunk)
                self._process.stdin.flush()
            except (BrokenPipeError, ValueError):
                return

    def _pump_device_into_whatever_is_connected(self) -> None:
        """Everything the device says goes to the open link, or nowhere.

        Nowhere is a real case and not an error: a device whose host has gone
        away carries on scanning and carries on talking, and what it emits into
        a closed link is lost. That is what link loss *is*, and it is why a
        reconnecting daemon drops whatever was buffered before it greets.
        """
        assert self._process is not None and self._process.stdout is not None
        while not self._stop.is_set():
            chunk = self._process.stdout.read(1)
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
        print(REASON_WHEN_NOT_BUILT, file=sys.stderr)
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
