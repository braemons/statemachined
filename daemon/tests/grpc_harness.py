# SPDX-License-Identifier: GPL-3.0-or-later
"""A daemon's gRPC interface, in this process, with a real client on it.

**Why a real server and not the servicers called directly.** A servicer is an
async generator taking a `context`, and calling one by hand means inventing a
context, inventing the trailing metadata, and deciding for yourself what
`abort` does. Every one of those inventions is a place where the test agrees
with itself rather than with grpcio. `grpc.aio` on a loopback port costs a few
milliseconds and removes all of them: the refusal a test sees is the refusal a
rig sees, trailers and all.

It also removes `WebSocketOverTheTestClient`, which existed because Starlette's
in-process WebSocket blocks for ever on a receive. A gRPC stream has a deadline
of its own, and cancelling the call is the whole of unsubscribing.

**The server runs on its own event loop, in its own thread.** Everything in
this package is synchronous on purpose — the client, the device layer, the
tests — and `grpc.aio` needs a loop. One thread owns that loop for the
harness's lifetime; nothing outside this module touches it.

In a module of its own rather than in a `conftest.py` for the reason
`rig_harness.py` gives: pytest prepends each test directory to `sys.path`, so
`from conftest import ...` means whichever `conftest.py` was imported first,
and a module named for what it holds means the same thing from anywhere.
"""

from __future__ import annotations

import asyncio
import socket
import threading

from statemachined.daemon.api.grpc_server import build_server
from statemachined.daemon.api.rig_service import RigService
from statemachined_client import StatemachinedClient

#: How long to wait for the server to bind, and for it to stop. Both are local
#: and immediate; the timeout is here so that a harness that wedges fails the
#: suite in seconds rather than hanging a CI job.
STARTUP_TIMEOUT_SECONDS = 10.0


def a_free_port() -> int:
    """Ask the kernel for one, and hand it over.

    There is a race between closing this socket and the server binding it, and
    it is the one every test harness accepts: the alternative is a fixed port,
    and a fixed port makes two runs of this suite on one machine collide —
    which is a certainty rather than a race.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class DaemonOnALoopbackPort:
    """One `RigService`, its servicers, and a gRPC server over both.

    The service is built here rather than passed in for the same reason
    `statemachined serve` builds one and hands it to both listeners: two
    `RigService` objects would be two daemons fighting over one serial port.
    """

    def __init__(self, configuration) -> None:
        self.service = RigService(configuration)
        self.port = a_free_port()
        self.address = f"127.0.0.1:{self.port}"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._stopped = threading.Event()

    # -- the lifetime -----------------------------------------------------------

    def start(self) -> None:
        """Start the link thread, then the server. In that order.

        The service first, because a server that accepted a call before the
        link thread existed would answer it out of a half-built daemon — which
        is a race a rig never has, since `statemachined serve` starts the
        service inside the app's lifespan before either listener binds.
        """
        self.service.start()
        self._thread = threading.Thread(target=self._serve, name="grpc-harness", daemon=True)
        self._thread.start()
        if not self._running.wait(STARTUP_TIMEOUT_SECONDS):
            raise AssertionError(f"the harness never listened on {self.address}")

    def stop(self) -> None:
        """Stop the server, then the service. The other order.

        Whatever is mid-call gets to finish against a daemon that still has a
        device; the reverse would abort a trial by tearing the link out from
        under an rpc that was reading it.
        """
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._stopped.wait(STARTUP_TIMEOUT_SECONDS)
        if self._thread is not None:
            self._thread.join(timeout=STARTUP_TIMEOUT_SECONDS)
        self.service.stop()

    def client(self, **kwargs) -> StatemachinedClient:
        """`statemachined-client`, pointed at this server.

        The published client with its published defaults — no injected
        transport and no test double — so that what a suite exercises is what
        somebody installs.
        """
        return StatemachinedClient(self.address, **kwargs)

    # -- the loop ---------------------------------------------------------------

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        server, self.servicers = build_server(self.service, self.address)
        try:
            loop.run_until_complete(server.start())
            self._running.set()
            loop.run_forever()
            # `grace=None` cancels whatever is in flight rather than waiting on
            # it. A stream is in flight by design here — a subscription is a
            # call that never ends — so waiting would mean waiting for ever.
            loop.run_until_complete(server.stop(None))
        finally:
            # Give the loop one pass to let the cancellations it just issued
            # land, so nothing is garbage-collected mid-await and printed as
            # "Task was destroyed but it is pending".
            loop.run_until_complete(asyncio.sleep(0))
            asyncio.set_event_loop(None)
            loop.close()
            self._running.set()  # so a failed start does not hang the wait
            self._stopped.set()
