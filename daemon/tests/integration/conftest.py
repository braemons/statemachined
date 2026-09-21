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

import pytest
from grpc_harness import DaemonOnALoopbackPort
from rig_harness import configuration_for

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
# `statemachined-client` against this daemon's own servicers, over a real gRPC
# channel on a loopback port. What this exercises that nothing below it can is
# the assembled thing: the servicers, the refusals as trailing metadata, the
# streams, and the client's seam — all of it the code a rig runs.
#
# **The client is the published one, with its published defaults.** There is no
# injected transport and no test double, because the failure this suite is for
# is precisely that the shipped client and the shipped daemon disagree.
#
# `tests/e2e/` is where the daemon is a *process*: `statemachined serve`, two
# listeners, a real socket. Neither can be reached from in-process, and neither
# is what these tests are about.


@pytest.fixture
def daemon(native_device, tmp_path):
    """The shipped servicers over the shipped service, wired to that device."""
    harness = DaemonOnALoopbackPort(configuration_for(native_device, tmp_path))
    harness.start()
    try:
        yield harness
    finally:
        harness.stop()


@pytest.fixture
def rig(daemon):
    """The client, pointed at that daemon.

    `wait_until_ready` before anything else: the harness has bound its port,
    but the channel has not connected, and a first call that raced that would
    fail as `unavailable` and say nothing about what it was testing.
    """
    client = daemon.client()
    client.wait_until_ready(timeout_s=10)
    with client:
        yield client
