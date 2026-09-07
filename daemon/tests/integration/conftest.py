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

So a small bridge -- daemon/bench/native_device_on_a_socket.py, shared with the
bench so there is only one of it -- pumps a TCP socket to the child's pipes, and
the daemon connects with `socket://127.0.0.1:<port>`. That is a URL a rig genuinely
uses -- an ethernet-attached MCU, which serial_link.py exists to make
indistinguishable -- and it means the transport under test is the transport the
daemon ships.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The bridge itself lives in daemon/bench/, because it is not a test: it is how
# anybody runs this daemon with no board on the desk (`make bench-device`).
# Keeping one implementation matters more here than keeping tests importable
# only from packages -- two bridges that drift are two different devices.
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "daemon" / "bench"))

from native_device_on_a_socket import (  # noqa: E402
    REASON_WHEN_NOT_BUILT,
    NativeDeviceOnASocket,
    the_native_device_is_built,
)


@pytest.fixture
def native_device(tmp_path):
    """A freshly booted device, listening. One per test, so state cannot leak.

    Its settings store is a file under the test's own directory, for the same
    reason: a device now remembers its wiring, its graph set and whether it
    should be running trials on its own, and a shared store would make one
    test's saved settings the next test's boot.
    """
    if not the_native_device_is_built():
        pytest.skip(REASON_WHEN_NOT_BUILT)
    device = NativeDeviceOnASocket(store_path=str(tmp_path / "store.bin"))
    device.start()
    try:
        yield device
    finally:
        device.stop()
