# SPDX-License-Identifier: GPL-3.0-or-later
"""A real device, on this machine, at the other end of a real transport.

`statemachined_native_device` is the firmware's own session and engine built for
the host (firmware/native/). These tests talk to it through the daemon's own
`SerialLink`, so what is exercised is the whole stack: the compiler, the
framing, the session's one-command-in-flight rule, the device's parser, its
validator, its scan loop, its result chunker.

**Why a socket and not a pty.** docs/developer/daemon.md said "over a pty", and a pty turns
out to be the awkward choice rather than the obvious one: the native device
takes its link on stdin and stdout, and pyserial opens a *path*, so the two ends
of a pty pair cannot both be reached that way -- the parent would have to bypass
`SerialLink` and use the master file descriptor raw, which is precisely the code
path a test should not be skipping.

So a small bridge -- `statemachined.device.native_device_on_a_socket`, shared
with the bench so there is only one of it -- pumps a TCP socket to the pipes, and
the daemon connects with `socket://127.0.0.1:<port>`. That is a URL a rig genuinely
uses -- an ethernet-attached MCU, which serial_link.py exists to make
indistinguishable -- and it means the transport under test is the transport the
daemon ships.
"""

from __future__ import annotations

import contextlib
import queue
import threading

import pytest
from fastapi.testclient import TestClient
from rig_harness import configuration_for
from statemachined.client import StatemachinedClient
from statemachined.daemon.api.application import create_application

# The bridge is part of the daemon, not part of these tests: it is how anybody
# runs this daemon with no board on the desk, from a checkout (`make
# bench-device`) or from a package (`statemachined device`). It was importable
# only by path until it became a shipped artifact; now it imports like anything
# else, and there is still only one of it -- two bridges that drift are two
# different devices.
from statemachined.device.native_device_on_a_socket import (
    NativeDeviceOnASocket,
    the_native_device_is_built,
)
from statemachined.device import native_device_on_a_socket


@pytest.fixture
def native_device(tmp_path):
    """A freshly booted device, listening. One per test, so state cannot leak.

    Its settings store is a file under the test's own directory, for the same
    reason: a device now remembers its wiring, its graph set and whether it
    should be running trials on its own, and a shared store would make one
    test's saved settings the next test's boot.
    """
    if not the_native_device_is_built():
        pytest.skip(native_device_on_a_socket.REASON_WHEN_NOT_BUILT)
    device = NativeDeviceOnASocket(store_path=str(tmp_path / "store.bin"))
    device.start()
    try:
        yield device
    finally:
        device.stop()


# ------------------------------------------------ the client, over that daemon ---
#
# `statemachined.client` against the same daemon, in the same process. What this
# adds over `test_http_api_against_native_device.py` is only the client: the
# daemon and the device beneath it are identical, so a failure here is the
# client's and a failure there is the daemon's, which is the whole reason both
# suites exist rather than one.
#
# `tests/e2e/` is where the client's *defaults* are exercised -- a real httpx
# connection and a real WebSocket handshake through uvicorn. Neither can be
# reached from in-process, and neither is what these tests are about.

#: What the client believes it is talking to. `TestClient` routes by path and
#: ignores the host, so this is arbitrary -- and deliberately not localhost, so
#: that a test asserting on a URL asserts on the client's own arithmetic rather
#: than on a default that happens to match.
RIG_URL = "http://statemachined.test"


@pytest.fixture
def daemon(native_device, tmp_path):
    """The shipped daemon, wired to that device, with stores of its own."""
    with TestClient(create_application(configuration_for(native_device, tmp_path))) as client:
        yield client


@pytest.fixture
def rig(daemon):
    """The client, pointed at that daemon.

    Starlette's `TestClient` **is** an `httpx.Client`, and that is the trick: it
    routes by path and ignores the host, so the client builds exactly the URL it
    would build on a real network and the request lands in the app in this
    process. `httpx.ASGITransport` cannot be used for it -- that one is
    async-only, and everything in this package is synchronous on purpose.

    The websocket factory is injected for the same reason from the other
    direction: `TestClient` runs a WebSocket route in this process too, and
    `WebSocketOverTheTestClient` below adapts its session to the one method a
    subscription needs.
    """
    client = StatemachinedClient(
        RIG_URL,
        timeout_seconds=30.0,
        http_client=daemon,
        open_websocket=lambda url: WebSocketOverTheTestClient(daemon, url),
    )
    with client:
        yield client


class WebSocketOverTheTestClient:
    """Starlette's in-process WebSocket, with a deadline on receiving.

    `WebSocketTestSession.receive_text()` blocks for ever, which is the right
    default for a test asserting on a frame it knows is coming and the wrong one
    for a subscription whose whole contract is that the deadline belongs to the
    subscriber. So one thread pumps frames into a queue and `recv` takes them
    with a timeout -- which is also what makes a *failing* test here fail in
    seconds with a message instead of hanging a CI job.
    """

    def __init__(self, test_client: TestClient, url: str) -> None:
        # ASGI routes by path; the ws:// host in the URL is the client's own
        # arithmetic and is not where this connects.
        path_and_query = url.split("://", 1)[-1].split("/", 1)[-1]
        self._session = test_client.websocket_connect("/" + path_and_query)
        self._socket = self._session.__enter__()
        self._frames: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump_frames, daemon=True).start()

    def _pump_frames(self) -> None:
        try:
            while True:
                self._frames.put(self._socket.receive_text())
        except Exception as exc:  # noqa: BLE001
            self._frames.put(exc)

    def recv(self, timeout: float | None = None):
        try:
            frame = self._frames.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("nothing arrived on the subscription") from None
        if isinstance(frame, Exception):
            raise frame
        return frame

    def close(self) -> None:
        # The daemon may have closed it first -- which it does, deliberately, to
        # a subscriber that fell out of the ring.
        with contextlib.suppress(Exception):
            self._session.__exit__(None, None, None)
