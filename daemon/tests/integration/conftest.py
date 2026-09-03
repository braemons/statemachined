# SPDX-License-Identifier: GPL-3.0-or-later
"""A real device, on this machine, at the other end of a real transport.

`statemachined_native_device` is the firmware's own session and engine built for
the host (firmware/native/). These tests talk to it through the daemon's own
`SerialLink`, so what is exercised is the whole stack: the compiler, the
framing, the session's one-command-in-flight rule, the device's parser, its
validator, its scan loop, its result chunker.

**Why a socket and not a pty.** dev/DAEMON.md said "over a pty", and a pty turns
out to be the awkward choice rather than the obvious one: the native device
takes its link on stdin and stdout, and pyserial opens a *path*, so the two ends
of a pty pair cannot both be reached that way -- the parent would have to bypass
`SerialLink` and use the master file descriptor raw, which is precisely the code
path a test should not be skipping.

So a small bridge in this file pumps a TCP socket to the child's pipes and the
daemon connects with `socket://127.0.0.1:<port>`. That is a URL a rig genuinely
uses -- an ethernet-attached MCU, which serial_link.py exists to make
indistinguishable -- and it means the transport under test is the transport the
daemon ships.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
NATIVE_DEVICE_BINARY = REPOSITORY_ROOT / "build" / "statemachined_native_device"

SKIP_REASON_WHEN_NOT_BUILT = (
    f"{NATIVE_DEVICE_BINARY} is not built. Run `make test` or `make integration-device` first; "
    "`make test-integration` does both."
)


class NativeDeviceUnderTest:
    """One child process, reachable at a TCP address.

    The bridge is two threads because the child's stdin and stdout are separate
    pipes and either may block: a single loop would deadlock the first time the
    device wrote a result while the test was still sending an upload.
    """

    def __init__(self) -> None:
        self._listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listening_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listening_socket.bind(("127.0.0.1", 0))
        self._listening_socket.listen(1)
        self.port = self._listening_socket.getsockname()[1]

        self._process: subprocess.Popen | None = None
        self._connection: socket.socket | None = None
        self._stop = threading.Event()

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
        things worth testing: the daemon closes the port, opens it again, and
        the device it comes back to is the same process with the same committed
        set.
        """
        while not self._stop.is_set():
            try:
                connection, _ = self._listening_socket.accept()
            except OSError:
                return
            self._connection = connection
            # Returns as soon as this connection closes, which is what a link
            # loss looks like from the device's side.
            self._pump_socket_into_device(connection)
            self._connection = None

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


@pytest.fixture
def native_device():
    """A freshly booted device, listening. One per test, so state cannot leak."""
    if not NATIVE_DEVICE_BINARY.exists() or not os.access(NATIVE_DEVICE_BINARY, os.X_OK):
        pytest.skip(SKIP_REASON_WHEN_NOT_BUILT)
    device = NativeDeviceUnderTest()
    device.start()
    try:
        yield device
    finally:
        device.stop()
