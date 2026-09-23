#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Both daemons, one script of calls, and every answer compared.

The port's check for the rpcs that need a board. Each daemon gets its own
freshly booted native device and an identical copy of the same stores, and the
real Python client -- the one experimenters' scripts import -- drives both
through the same calls in the same order. What comes back is compared: the
answer where there is one, and where there is a refusal, its status, `error`,
`context` and sentence.

**Through the client, not grpcurl**, because the client is the contract. It
reads the `statemachined-error-bin` trailer to tell "the daemon has no board"
from "nothing answered", and a daemon that sent the status without the trailer
would pass a comparison of status codes and fail every experimenter's script.

Run it from a checkout with the native device built (`make integration-device`)
and the Rust daemon built (`make rust`):

    uv run --project daemon python tools/compare_daemons.py

It prints each call with both answers when they differ, and exits non-zero if
any did.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "daemon" / "src"))
sys.path.insert(0, str(HERE / "client" / "python"))

from statemachined.device.native_device_on_a_socket import (  # noqa: E402
    NativeDeviceOnASocket,
    the_native_device_is_built,
)
from statemachined_client import StatemachinedClient  # noqa: E402
from statemachined_client.daemon_refusals import DaemonRefusedTheRequest  # noqa: E402

RUST_DAEMON = HERE / "target" / "debug" / "statemachined"

#: Fields that measure this run rather than describe the answer. Compared for
#: whether they are there, not for what they say.
DIFFERS_BY_RUN = {"elapsed_milliseconds", "opened_at_unix_seconds", "open_seconds"}


def a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def a_graph_too_big_for_the_board() -> dict:
    """Forty states in a line: valid, and eight more than the board holds."""
    states = [
        {"name": f"s{i}", "transitions": [{"when": {"all": ["lever"]}, "goto": f"s{i + 1}"}]}
        for i in range(39)
    ]
    states.append({"name": "s39", "outcome": "HIT"})
    return {"name": "too-big", "entry": "s0", "states": states}


def stores_in(root: Path) -> None:
    """The shipped graphs and the native device's config, and one that cannot fit."""
    (root / "graphs").mkdir(parents=True)
    (root / "configs").mkdir()
    for graph in (HERE / "graphs").glob("*.json"):
        shutil.copy(graph, root / "graphs")
    (root / "graphs" / "too-big.json").write_text(json.dumps(a_graph_too_big_for_the_board()))
    shutil.copy(HERE / "configs" / "native-device.config.json", root / "configs")


def rig_config(root: Path, target: str) -> Path:
    """Neither daemon connects or loads anything on its own: the script does
    both, so the two start from the same nothing."""
    path = root / "rig-config.toml"
    path.write_text(
        f'device_target = "{target}"\n'
        "connect_on_startup = false\n"
        'expected_board = ""\n'
        'startup_state_machine_config = ""\n'
        f'graph_store_directory = "{root / "graphs"}"\n'
        f'state_machine_config_directory = "{root / "configs"}"\n'
        f'trace_directory = "{root / "trace"}"\n'
        f'recording_directory = "{root / "recordings"}"\n'
    )
    return path


class Daemon:
    """One daemon process, its own device, and a client on it."""

    def __init__(self, which: str, root: Path) -> None:
        self.which = which
        stores_in(root)
        self.device = NativeDeviceOnASocket(store_path=str(root / "device-store.bin"))
        self.device.start()
        config = rig_config(root, self.device.target_url)
        port = a_free_port()
        log = open(root / "daemon.log", "w")
        if which == "rust":
            command = [str(RUST_DAEMON), "--config", str(config), "--port", str(port)]
            grpc_port = port
        else:
            command = [
                "uv", "run", "--project", str(HERE / "daemon"), "statemachined",
                "-t", self.device.target_url,
                "serve", "--config", str(config), "--host", "127.0.0.1",
                "--port", str(port), "--no-mdns",
            ]
            # The Python daemon serves gRPC on the port after the panels'.
            grpc_port = port + 1
        self.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        self.client = StatemachinedClient(f"127.0.0.1:{grpc_port}")
        self.client.wait_until_ready(timeout_s=30.0)

    def stop(self) -> None:
        self.client.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.device.stop()


def plain(value):
    """An answer as plain data, with what differs by run taken out."""
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        return {
            k: (v is not None) if k in DIFFERS_BY_RUN else plain(v) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def answer(call):
    try:
        return {"answered": plain(call())}
    except DaemonRefusedTheRequest as refusal:
        return {
            "refused": {
                "status": refusal.status,
                "error": refusal.error,
                "context": refusal.context,
                "detail": refusal.detail,
            }
        }


#: The script, in order: each step is a name and what to ask. A step that
#: reads a larger message narrows it to the part that is ported, since the
#: rest belongs to rpcs that are not.
SCRIPT = [
    ("the session, before anything", lambda c: c.read_session()),
    ("closing nothing", lambda c: c.close_session()),
    ("an active graph with no config", lambda c: c.set_active_graph("go-nogo")),
    ("clearing an active graph nobody set", lambda c: c.clear_active_graph()),
    ("uploading with no board", lambda c: c.upload_graph_set(["go-nogo"])),
    ("opening with no board", lambda c: c.open_session()),
    ("one graph with no board", lambda c: c.upload_graph("go-nogo")),
    ("the link", lambda c: c.open_link().connected),
    ("opening with no config loaded", lambda c: c.open_session()),
    ("loading a config nobody stored", lambda c: c.load_config("nope")),
    ("loading the native device's config", lambda c: c.load_config("native-device")),
    ("the session, loaded and not open", lambda c: c.read_session()),
    ("an active graph, nothing on the board", lambda c: c.set_active_graph("go-nogo")),
    ("an active graph the config has not got", lambda c: c.set_active_graph("nope")),
    ("an empty active graph", lambda c: c.set_active_graph("")),
    ("one graph nobody stored", lambda c: c.upload_graph("nope")),
    ("a set that does not fit", lambda c: c.upload_graph_set(["go-nogo", "too-big"])),
    ("a set naming a graph nobody stored", lambda c: c.upload_graph_set(["go-nogo", "nope"])),
    ("one graph", lambda c: c.upload_graph("go-nogo")),
    ("the committed set, on the device", lambda c: c.read_device().committed_set),
    ("opening from the loaded config", lambda c: c.open_session()),
    ("one graph over a session's set", lambda c: c.upload_graph("state-walk")),
    ("a set by name", lambda c: c.upload_graph_set(["state-walk", "go-nogo"])),
    ("the committed set, again", lambda c: c.read_device().committed_set),
    ("an active graph the board is not holding",
     lambda c: c.set_active_graph("two-alternative-forced-choice")),
    ("an active graph the board is holding", lambda c: c.set_active_graph("state-walk")),
    ("the session, open", lambda c: c.read_session()),
    ("closing it", lambda c: c.close_session()),
    ("closing it again", lambda c: c.close_session()),
    ("clearing the active graph", lambda c: c.clear_active_graph()),
    ("the session, closed with a set on the board", lambda c: c.read_session()),
    ("deleting the loaded config", lambda c: c.delete_config("native-device")),
    ("the session, its config no longer stored", lambda c: c.read_session()),
    ("an empty set", lambda c: c.upload_graph_set([])),
]


def main() -> int:
    if not the_native_device_is_built():
        print("the native device is not built: make integration-device")
        return 2
    if not RUST_DAEMON.exists():
        print("the Rust daemon is not built: make rust")
        return 2

    with tempfile.TemporaryDirectory() as scratch:
        daemons = {}
        try:
            for which in ("python", "rust"):
                daemons[which] = Daemon(which, Path(scratch) / which)
            disagreed = 0
            for why, call in SCRIPT:
                python = answer(lambda: call(daemons["python"].client))
                rust = answer(lambda: call(daemons["rust"].client))
                if python == rust:
                    kind = "refused" if "refused" in python else "answered"
                    print(f"  same   {why}  ({kind})")
                    if "--show" in sys.argv:
                        print(f"           {json.dumps(python, sort_keys=True)}")
                    continue
                disagreed += 1
                print(f"  DIFFER {why}")
                print(f"           python: {json.dumps(python, sort_keys=True)}")
                print(f"           rust:   {json.dumps(rust, sort_keys=True)}")
        finally:
            for daemon in daemons.values():
                daemon.stop()
    print(f"{len(SCRIPT) - disagreed}/{len(SCRIPT)} calls agree")
    return 1 if disagreed else 0


if __name__ == "__main__":
    raise SystemExit(main())
