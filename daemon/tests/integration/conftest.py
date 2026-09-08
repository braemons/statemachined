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

So a small bridge -- `statemachined.device.native_device_on_a_socket`, shared
with the bench so there is only one of it -- pumps a TCP socket to the pipes, and
the daemon connects with `socket://127.0.0.1:<port>`. That is a URL a rig genuinely
uses -- an ethernet-attached MCU, which serial_link.py exists to make
indistinguishable -- and it means the transport under test is the transport the
daemon ships.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from statemachined.api.application import create_application
from statemachined.rig_configuration import RigConfiguration

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


# ------------------------------------------------------- the shared harness ---
#
# Below the device fixture: what every integration module needs to stand a
# daemon up in front of it. Here rather than in one test module because there is
# more than one of those now -- the end-to-end suite runs the same daemon
# against the same device, and two copies of `configuration_for` would be two
# subtly different rigs.


def timed_graph(name: str, milliseconds: int) -> dict:
    return {
        "name": name,
        "entry": "Wait",
        "distributions": {"dwell": {"kind": "fixed", "duration_ms": milliseconds}},
        "states": [
            {
                "name": "Wait",
                "on_entry": [{"line": "ready_lamp", "kind": "high"}],
                "timeout": {"after": "dwell", "goto": "Hit"},
            },
            {"name": "Hit", "outcome": "HIT"},
        ],
    }


#: The rig this fixture pretends to be, as a state-machine config. Written to
#: the store below and named as the startup config, because that is how a real
#: rig gets its line map now -- the rig config holds none, and a daemon with no
#: config loaded has no lines at all.
BENCH_LINE_MAP = {
    "input_lines": [
        {"name": "start_switch", "line_index": 0},
        {"name": "lever", "line_index": 4},
    ],
    "output_lines": [
        {"name": "ready_lamp", "line_index": 0},
        {"name": "reward_valve", "line_index": 3, "safe_level_is_high": True},
    ],
}


def write_state_machine_config(directory, name="bench", line_map=None, graphs=()):
    """One config in the store, as the daemon would find it on disk."""
    directory.mkdir(parents=True, exist_ok=True)
    body = {
        "name": name,
        "line_map": BENCH_LINE_MAP if line_map is None else line_map,
        "graphs": list(graphs),
    }
    (directory / f"{name}.config.json").write_text(json.dumps(body, indent=2) + "\n")
    return name


def configuration_for(native_device, tmp_path, **overrides) -> RigConfiguration:
    """A daemon wired to this device, with stores and a trace of its own."""
    config_directory = tmp_path / "configs"
    # `line_map` names a state-machine config's contents, not a rig config's --
    # the rig config has no such field any more -- so it is taken out of the
    # overrides and written to the store the daemon will load from.
    write_state_machine_config(config_directory, line_map=overrides.pop("line_map", None))
    settings = {
        "device_target": native_device.target_url,
        "device_timeout_seconds": 5.0,
        "graph_store_directory": tmp_path / "graphs",
        "state_machine_config_directory": config_directory,
        "trace_directory": tmp_path / "trace",
        "recording_directory": tmp_path / "recordings",
        "heartbeat_seconds": 0.5,
        "startup_state_machine_config": "bench",
    }
    settings.update(overrides)
    return RigConfiguration(**settings)


def wait_until(predicate, timeout_seconds: float = 5.0):
    """Poll for something the link thread will do soon.

    A result and the visit stream arrive unasked, so a test that asserted
    immediately after `start` would be asserting on a race rather than on the
    daemon.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return None
