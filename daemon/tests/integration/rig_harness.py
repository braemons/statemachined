# SPDX-License-Identifier: GPL-3.0-or-later
"""The bench rig every integration suite stands up, in a module of its own.

Split out of `conftest.py` rather than left in it for two reasons, and only the
second is new.

**One definition of the bench.** Two suites now drive a daemon in front of the
native device -- the daemon's own, and the client's -- and a second copy of
`timed_graph` or of the line map would be a second rig that drifts from this one
silently. The point of the client's tests is that they meet the same far end the
daemon's tests do.

**`from conftest import ...` stops being safe once there is more than one.**
pytest prepends each test directory to `sys.path`, so with a `conftest.py` under
both `unit/` and `integration/` the name means whichever was imported first. A
module named for what it holds means the same thing from anywhere.
"""

from __future__ import annotations

import json
import time

from bench_rig import BENCH_LINE_MAP, timed_graph  # noqa: F401  re-exported
from statemachined.daemon.rig_configuration import RigConfiguration

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
