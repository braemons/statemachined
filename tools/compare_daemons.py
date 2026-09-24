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
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "daemon" / "src"))
sys.path.insert(0, str(HERE / "client" / "python"))

from statemachined.device.native_device_on_a_socket import (  # noqa: E402
    NativeDeviceOnASocket,
    the_native_device_is_built,
)
from statemachined_client import StatemachinedClient  # noqa: E402
from statemachined_client.api_types import (  # noqa: E402
    DistributionPatch,
    RigConfigurationPatch,
)
from statemachined_client.daemon_refusals import DaemonRefusedTheRequest  # noqa: E402

RUST_DAEMON = HERE / "target" / "debug" / "statemachined"

#: Fields that measure this run rather than describe the answer. Compared for
#: whether they are there, not for what they say.
DIFFERS_BY_RUN = {
    "elapsed_milliseconds",
    "opened_at_unix_seconds",
    "open_seconds",
    "recorded_host_time",
    "connected_at_unix_seconds",
    "connected_seconds",
    "uptime_device_microseconds",
    # A trial's timings are the device's clock, and two devices started at two
    # moments measure the same run to different microseconds. The *draws* are
    # compared: both daemons use one seed, so they must agree exactly.
    "started_device_microseconds",
    "entered_device_microseconds",
    "measured_duration_microseconds",
    "total_duration_microseconds",
    "unwrapped_device_microseconds",
    "entered_host_time",
    "host_time_uncertainty_microseconds",
}


def a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


#: Two graphs that need no hand on a lever: one that walks through random
#: timeouts to HIT, so the seeded draws can be compared, and one that waits for
#: a lever nobody presses, so there is something to cancel.
GRAPHS_FOR_TRIALS = [
    {
        "name": "timed-walk",
        "entry": "Wait",
        "distributions": {
            "wait": {"kind": "uniform", "minimum_ms": 20, "maximum_ms": 80},
            "step": {"kind": "choice", "options_ms": [10, 30, 50], "weights": [1, 2, 1]},
        },
        "states": [
            {"name": "Wait", "timeout": {"after": "wait", "goto": "Step"},
             "transitions": [{"when": {"all": ["lever"]}, "goto": "Early"}]},
            {"name": "Step", "timeout": {"after": "step", "goto": "Gate"}},
            # Left by its *second* transition, at once: nobody presses the
            # lever, so `none` holds on entry. A transition index to decode.
            {"name": "Gate", "transitions": [
                {"when": {"all": ["lever"]}, "goto": "Early"},
                {"when": {"none": ["lever"]}, "goto": "Done",
                 "fire_if_already_true_on_entry": True},
            ]},
            {"name": "Done", "outcome": "HIT"},
            {"name": "Early", "outcome": "EARLY"},
        ],
    },
    {
        "name": "waits-for-ever",
        "entry": "Wait",
        "states": [
            {"name": "Wait", "transitions": [{"when": {"all": ["lever"]}, "goto": "Done"}]},
            {"name": "Done", "outcome": "HIT"},
        ],
    },
]


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
    # The same rig written by index rather than by pin: what a line reports as
    # its label when the config never named one is a separate answer.
    by_index = json.loads((HERE / "configs" / "native-device.config.json").read_text())
    by_index["name"] = "by-index"
    for direction in ("input_lines", "output_lines"):
        for line in by_index["line_map"][direction]:
            line["line_index"] = int(line.pop("pin_label").removeprefix("sim"))
    (root / "configs" / "by-index.config.json").write_text(json.dumps(by_index))
    for graph in GRAPHS_FOR_TRIALS:
        (root / "graphs" / f"{graph['name']}.json").write_text(json.dumps(graph))


def rig_config(root: Path, target: str, startup: dict) -> Path:
    """By default neither daemon connects or loads anything on its own: the
    script does both, so the two start from the same nothing. `startup` says
    otherwise, for the runs about starting."""
    path = root / "rig-config.toml"
    connect = "true" if startup.get("connect") else "false"
    path.write_text(
        f'device_target = "{target}"\n'
        f"connect_on_startup = {connect}\n"
        'expected_board = ""\n'
        # One seed for both, so a trial draws the same numbers on each.
        'session_seed = "00C0FFEE00C0FFEE"\n'
        # Long, so the link thread's reading between requests is the only thing
        # that brings a result in promptly. The native device notices a lost
        # link by the port closing, not by a missed ping.
        "heartbeat_seconds = 30.0\n"
        f'startup_state_machine_config = "{startup.get("config", "")}"\n'
        f'graph_store_directory = "{root / "graphs"}"\n'
        f'state_machine_config_directory = "{root / "configs"}"\n'
        f'trace_directory = "{root / "trace"}"\n'
        f'recording_directory = "{root / "recordings"}"\n'
    )
    return path


class Daemon:
    """One daemon process, its own device, and a client on it."""

    def __init__(self, which: str, root: Path, startup: dict) -> None:
        self.which = which
        stores_in(root)
        self.device = NativeDeviceOnASocket(store_path=str(root / "device-store.bin"))
        self.device.start()
        config = rig_config(root, self.device.target_url, startup)
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


#: What is each daemon's own in any string: its scratch directory and its
#: device's port.
OWN_PATH = re.compile(r"/tmp/[^/\s]+/(python|rust)(?=/)")
OWN_SOCKET = re.compile(r"(socket://127\.0\.0\.1):\d+")


def plain(value):
    """An answer as plain data, with what differs by run taken out."""
    if isinstance(value, str):
        return OWN_SOCKET.sub(r"\1", OWN_PATH.sub("<scratch>", value))
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        if isinstance(value.get("address"), str):
            # The peer's port is the client's ephemeral one; the host is the
            # part both daemons can be asked to agree on.
            value = {**value, "address": value["address"].rsplit(":", 1)[0]}
        if isinstance(value.get("target"), str):
            # Each daemon's own device socket.
            value = {**value, "target": value["target"].rsplit(":", 1)[0]}
        return {
            k: (v is not None) if k in DIFFERS_BY_RUN else plain(v) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def first_differences(python, rust, where="", limit=5):
    """The first few places two answers part, so a long one can be read."""
    found = []
    if isinstance(python, dict) and isinstance(rust, dict):
        for key in list(python) + [k for k in rust if k not in python]:
            found += first_differences(python.get(key), rust.get(key), f"{where}.{key}", limit)
    elif isinstance(python, list) and isinstance(rust, list) and len(python) == len(rust):
        for index, (theirs, ours) in enumerate(zip(python, rust)):
            found += first_differences(theirs, ours, f"{where}[{index}]", limit)
    elif python != rust:
        found.append((where or "the answer", (python, rust)))
    return found[:limit]


PARSER_WORDS = {"bad_graph", "bad_state_machine_config", "bad_line_map"}


def a_line_map(**renames) -> str:
    """The native device's own map, with some lines renamed or re-pinned."""
    line_map = json.loads(
        (HERE / "configs" / "native-device.config.json").read_text()
    )["line_map"]
    for direction in ("input_lines", "output_lines"):
        for line in line_map[direction]:
            line.update(renames.get(line["name"], {}))
    return json.dumps(line_map)

#: A graph that is legal and probably not what its author meant: a line in
#: both `all` and `any` makes every other line in `any` moot.
A_DRAFT_WITH_A_WARNING = json.dumps({
    "name": "draft",
    "entry": "Wait",
    "states": [
        {"name": "Wait", "transitions": [
            {"when": {"all": ["lever"], "any": ["lever", "abort"]}, "goto": "Done"}]},
        {"name": "Done", "outcome": "HIT"},
    ],
})


def answer(call):
    try:
        return {"answered": plain(call())}
    except DaemonRefusedTheRequest as refusal:
        # A document that does not parse is refused in the parser's own
        # words -- pydantic's or serde's -- and the port holds the two to the
        # same verdict, not the same sentence (RUST_PORT.md 4.2).
        if refusal.error in PARSER_WORDS:
            return {"refused": {"status": refusal.status, "error": refusal.error,
                                "context": refusal.context, "detail": "(the parser's)"}}
        return {
            "refused": plain({
                "status": refusal.status,
                "error": refusal.error,
                "context": refusal.context,
                "detail": refusal.detail,
            })
        }


def watching_the_whole_trace(client):
    """Open WatchTrace from 0, take the backlog, and ask who is watching while
    the stream is still open."""
    backlog = client.read_trace().newest_entry_number + 1
    # A deadline, so a stream that never sends the backlog is a disagreement
    # to report rather than a comparison that hangs.
    with client.watch_trace(0, observer="compare-daemons", timeout_s=5.0) as subscription:
        received = []
        for entry in subscription:
            received.append(entry)
            if len(received) == backlog:
                break
        observers = client.read_observers()
    return {"entries": received, "observers": observers}


def the_observers_once_it_closed(client):
    # Unregistering happens when the server notices the cancel, which is not
    # the instant the client sends it.
    time.sleep(0.5)
    return client.read_observers()


#: The script, in order: each step is a name and what to ask. A step that
#: reads a larger message narrows it to the part that is ported, since the
#: rest belongs to rpcs that are not.
SCRIPT = [
    ("the session, before anything", lambda c: c.read_session()),
    ("closing nothing", lambda c: c.close_session()),
    ("an active graph with no config", lambda c: c.set_active_graph("go-nogo")),
    ("clearing an active graph nobody set", lambda c: c.clear_active_graph()),
    ("the firmware with no board", lambda c: c.read_firmware()),
    ("validating with no board", lambda c: c.validate_graph("go-nogo")),
    ("uploading with no board", lambda c: c.upload_graph_set(["go-nogo"])),
    ("opening with no board", lambda c: c.open_session()),
    ("one graph with no board", lambda c: c.upload_graph("go-nogo")),
    ("the link", lambda c: c.open_link().connected),
    ("the firmware", lambda c: c.read_firmware()),
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
    ("deleting the other one", lambda c: c.delete_config("by-index")),
    ("the session, its config no longer stored", lambda c: c.read_session()),
    ("the observers, nobody watching", lambda c: c.read_observers()),
    ("the trace, all of it", lambda c: c.read_trace()),
    ("the trace from entry 3, two of it", lambda c: c.read_trace(3, 2)),
    ("one trial's trace, with no trials", lambda c: c.read_trial_trace(1)),
    ("watching the trace, and who is watching", watching_the_whole_trace),
    ("the observers once it closed", the_observers_once_it_closed),
    ("the device, whole", lambda c: c.read_device()),
    ("an empty set", lambda c: c.upload_graph_set([])),
]

def the_wire(entries) -> list:
    """What crossed the port: every line the daemon sent, as sent, and the
    message type of every line it received -- whose bodies carry the device's
    own clock and so differ between any two runs."""
    seen = []
    for entry in entries:
        if entry.direction == "to_device":
            seen.append(["to_device", entry.line])
        else:
            kind = json.loads(entry.line).get("msg_type") if entry.line.startswith("{") else None
            seen.append(["from_device", kind])
    return seen


def watching_the_wire(client):
    backlog = client.read_serial_monitor().newest_entry_number + 1
    with client.watch_serial_monitor(0, timeout_s=5.0) as subscription:
        received = []
        for entry in subscription:
            received.append(entry)
            if len(received) == backlog:
                break
    return the_wire(received)


#: What a daemon is after starting, told to load a config and connect.
AFTER_A_STARTUP = [
    ("the session, as started", lambda c: c.read_session()),
    ("the trace, as started", lambda c: c.read_trace()),
    ("the device, as started", lambda c: c.read_device()),
    ("the lines, as started", lambda c: c.read_lines()),
    ("the wire, as started", lambda c: the_wire(c.read_serial_monitor().entries)),
    ("the wire, watched", watching_the_wire),
    ("the monitor's window past its start",
     lambda c: {k: v for k, v in vars(c.read_serial_monitor(3, 2)).items() if k != "entries"}),
    ("a line map that does not parse", lambda c: c.write_line_map("x", "{not json")),
    ("a line map naming a pin the board has not got",
     lambda c: c.write_line_map("x", a_line_map(lever={"pin_label": "D99"}))),
    ("the lines, after that was refused", lambda c: c.read_lines()),
    ("a line map that renames the lever",
     lambda c: c.write_line_map("x", a_line_map(lever={"name": "paw"}))),
    ("the session, with the map edited in memory", lambda c: c.read_session()),
    ("loading a config written by index", lambda c: c.load_config("by-index")),
    ("the lines, by index", lambda c: c.read_lines()),
]

def the_result_of(trial_id):
    """Wait for that trial's result to be the last one, then read it."""
    def call(client):
        # Well inside the heartbeat: a result that only arrived because a ping
        # happened to collect it is a daemon that does not read its link.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                result = client.read_trial_result()
                if result.trial_id == trial_id:
                    return result
            except DaemonRefusedTheRequest:
                pass
            time.sleep(0.05)
        return client.read_trial_result(trial_id)
    return call


def the_first_state_frame(client):
    with client.watch_state(timeout_s=5.0) as subscription:
        for frame in subscription:
            return frame


#: A session of trials, on a board and a config loaded at startup.
TRIALS = [
    ("a result before any trial", lambda c: c.read_trial_result()),
    ("validating a stored graph", lambda c: c.validate_graph("go-nogo")),
    ("validating one that does not fit", lambda c: c.validate_graph("too-big")),
    ("validating one nobody stored", lambda c: c.validate_graph("nope")),
    ("validating a draft with a warning", lambda c: c.validate_graph_text(A_DRAFT_WITH_A_WARNING)),
    ("validating a draft that does not parse", lambda c: c.validate_graph_text("{not json")),
    ("validating a draft that parses and breaks a rule",
     lambda c: c.validate_graph_text(json.dumps({"name": "x", "entry": "Nowhere", "states": []}))),
    ("a trial naming no graph, none active", lambda c: c.configure_trial(1)),
    ("a trial before any set", lambda c: c.configure_trial(1, graph="timed-walk")),
    ("the set for trials", lambda c: c.upload_graph_set(["timed-walk", "waits-for-ever"])),
    ("a negative trial id", lambda c: c.configure_trial(-1, graph="timed-walk")),
    ("a patch naming nothing",
     lambda c: c.configure_trial(1, graph="timed-walk",
                                 distribution_patches=[DistributionPatch(name="")])),
    ("a patch setting nothing",
     lambda c: c.configure_trial(1, graph="timed-walk",
                                 distribution_patches=[DistributionPatch(name="wait")])),
    ("a patch for a distribution the graph has not got",
     lambda c: c.configure_trial(1, graph="timed-walk",
                                 distribution_patches=[DistributionPatch(name="nope", minimum_ms=5)])),
    ("a graph the set has not got", lambda c: c.configure_trial(1, graph="go-nogo")),
    ("starting what was never armed", lambda c: c.start_trial(9)),
    ("arming trial 1", lambda c: c.configure_trial(1, graph="timed-walk")),
    ("starting it", lambda c: c.start_trial(1)),
    ("its result", the_result_of(1)),
    ("its trace", lambda c: c.read_trial_trace(1)),
    ("arming trial 2, which waits for ever",
     lambda c: c.configure_trial(2, graph="waits-for-ever", cap_milliseconds=60000)),
    ("starting it", lambda c: c.start_trial(2)),
    ("the rig while it waits", lambda c: (time.sleep(0.2), c.read_state())[1]),
    ("the first state frame while it waits", the_first_state_frame),
    ("cancelling it", lambda c: c.cancel_trial(2)),
    ("its result", the_result_of(2)),
    ("the result of a trial that is not the last", lambda c: c.read_trial_result(1)),
    ("trial 3, patched",
     lambda c: c.configure_trial(3, graph="timed-walk", distribution_patches=[
         DistributionPatch(name="wait", minimum_ms=5, maximum_ms=5)])),
    ("starting it", lambda c: c.start_trial(3)),
    ("its result", the_result_of(3)),
    ("deleting a graph the board is holding", lambda c: c.delete_graph("timed-walk")),
    ("deleting one it is not", lambda c: c.delete_graph("state-walk")),
    ("the rig after", lambda c: c.read_state()),
    ("closing, with trial 3 still the armed one", lambda c: c.close_session()),
    ("what closing left in the device", lambda c: c.read_device().link),
    ("the trace, every trial in it", lambda c: c.read_trace()),
    ("a patch that changes nothing", lambda c: c.patch_configuration(RigConfigurationPatch())),
    ("a graph mode the daemon has not got",
     lambda c: c.patch_configuration(RigConfigurationPatch(graph_mode="banana"))),
    ("a seed, a baud and a mode",
     lambda c: c.patch_configuration(RigConfigurationPatch(
         session_seed="0000000000000042", device_baud=57600, graph_mode="per_trial"))),
    ("expecting the board that is there, which re-greets it",
     lambda c: c.patch_configuration(RigConfigurationPatch(expected_board="native"))),
    ("the link, after the re-greeting", lambda c: c.read_device().link),
    # Greeted with the patched seed, so this trial draws from it.
    ("arming trial 4 on the patched seed", lambda c: c.configure_trial(4, graph="timed-walk")),
    ("starting it", lambda c: c.start_trial(4)),
    ("its result", the_result_of(4)),
    ("expecting a board that is not there",
     lambda c: c.patch_configuration(RigConfigurationPatch(expected_board="teensy41"))),
    ("the configuration it left", lambda c: c.read_configuration()),
    ("the device it left", lambda c: c.read_device().connected),
    ("the trace's last entries", lambda c: [e.kind for e in c.read_trace().entries[-6:]]),
]

#: Each run: how the daemons start, and what to ask them.
RUNS = [
    ("started with nothing", {}, SCRIPT),
    ("started with a config, connecting",
     {"config": "native-device", "connect": True}, AFTER_A_STARTUP),
    ("started with a config nobody stored, connecting",
     {"config": "nope", "connect": True}, AFTER_A_STARTUP),
    ("trials", {"config": "native-device", "connect": True}, TRIALS),
]


def main() -> int:
    if not the_native_device_is_built():
        print("the native device is not built: make integration-device")
        return 2
    if not RUST_DAEMON.exists():
        print("the Rust daemon is not built: make rust")
        return 2

    disagreed = 0
    asked = 0
    for run, startup, script in RUNS:
        print(run)
        with tempfile.TemporaryDirectory() as scratch:
            daemons = {}
            try:
                for which in ("python", "rust"):
                    daemons[which] = Daemon(which, Path(scratch) / which, startup)
                for why, call in script:
                    asked += 1
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
                    for where, (theirs, ours) in first_differences(python, rust):
                        print(f"           at {where}: python {theirs!r}, rust {ours!r}")
            finally:
                for daemon in daemons.values():
                    daemon.stop()
    print(f"{asked - disagreed}/{asked} calls agree")
    return 1 if disagreed else 0


if __name__ == "__main__":
    raise SystemExit(main())
