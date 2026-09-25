# SPDX-License-Identifier: LGPL-3.0-or-later
"""A board, a daemon in front of it, and a client: what the hardware suite drives.

The suite talks to the board the way everything else on a rig does -- through
statemachined, with this client -- so what it measures is what a session gets.
A trial here is a graph document, committed as a set, armed and started over
gRPC, and read back as the `TrialResult` a script would read.

Named for what it holds rather than `conftest`, so that importing it means the
same thing from every test module.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from statemachined_client import StatemachinedClient, TrialResult

#: The repository root, three levels up from this file.
REPOSITORY = Path(__file__).resolve().parents[4]

#: The daemon a checkout builds, and the firmware built for this machine.
BUILT_DAEMON = REPOSITORY / "target" / "debug" / "statemachined"
BUILT_NATIVE_DEVICE = REPOSITORY / "build" / "statemachined_native_device"

#: dev/PLAN.md M3, and the target docs/operations/bringup.md §4 reads off a board.
SCAN_HZ_TARGET = 10_000

#: The board's scan timer, from `kScanHz` in firmware/src/main.cpp. One period
#: is 100 us, and it is the unit most figures in this suite are expressed in.
SCAN_PERIOD_MICROSECONDS = 100


# ------------------------------------------------------- the loopback harness ---
#
# Eight jumper wires, output line *n* to input line *(n + 4) mod 8*, which is
# what docs/operations/hardware.md calls the loopback harness. The native device
# wires the same eight in software (STATEMACHINED_LOOPBACK=8).
#
# **Why the +4 shift and not out n -> in n.** A straight-through harness cannot
# distinguish a correct board from one whose reported input word is secretly the
# output word: raise output 0, see bit 0 in the input word, pass. Under the shift
# every output has a unique and non-obvious expected input bit, so that failure
# -- and any rotation or off-by-one in either pin table -- shows up as a test
# that fails rather than one that passes for the wrong reason.

#: output line -> input line, as the eight jumpers wire them.
LOOPBACK = {n: (n + 4) % 8 for n in range(8)}

#: The same thing as pins, for a message somebody can hold against a breadboard.
LOOPBACK_PINS = {
    "uno_r4_minima": [
        ("D10", "D6"),
        ("D11", "D7"),
        ("D12", "D8"),
        ("A0", "D9"),
        ("A1", "D2"),
        ("A2", "D3"),
        ("A3", "D4"),
        ("A4", "D5"),
    ],
}


def wiring_instructions(board: str) -> str:
    """The eight wires as a list, for a skip reason that is also a wiring guide."""
    pins = LOOPBACK_PINS.get(board, [])
    if not pins:
        return "\n".join(
            f"  output line {out} -> input line {inp}" for out, inp in sorted(LOOPBACK.items())
        )
    return "\n".join(
        f"  {frm:>3} (output {out})  ->  {to:>3} (input {inp})"
        for (out, inp), (frm, to) in zip(sorted(LOOPBACK.items()), pins, strict=True)
    )


def output(line: int) -> str:
    """The name the suite's line map gives output line `line`."""
    return f"out{line}"


def input_fed_by(line: int) -> str:
    """The input line the loopback harness wires output `line` to, by name."""
    return f"in{LOOPBACK[line]}"


# ------------------------------------------------------------- graph documents ---


def a_dwell_of(name: str, milliseconds: int) -> dict:
    """One state that holds for a fixed time, then ends. Nothing else."""
    return {
        "name": name,
        "entry": "Dwell",
        "distributions": {"dwell": {"kind": "fixed", "duration_ms": milliseconds}},
        "states": [
            {"name": "Dwell", "timeout": {"after": "dwell", "goto": "Done"}},
            {"name": "Done", "outcome": "HIT"},
        ],
    }


def a_line_answering_itself(name: str, outputs: list[int], *, mid_trial: bool) -> dict:
    """A state that raises `outputs` and waits for every input they drive.

    The measured duration of that state is the whole response path: the entry
    action reaching a pin, the jumper, the pin being read, the conditioner
    accepting it, and the predicate firing. `mid_trial` reaches it from a 20 ms
    dwell rather than from the `start` command, which is what takes the link
    out of the measurement.
    """
    answer = {
        "name": "Answer",
        "on_entry": [{"line": output(line), "kind": "high"} for line in outputs],
        "transitions": [
            {"when": {"all": [input_fed_by(line) for line in outputs]}, "goto": "Done"}
        ],
    }
    states = [answer, {"name": "Done", "outcome": "HIT"}]
    distributions = {}
    entry = "Answer"
    if mid_trial:
        distributions["lead"] = {"kind": "fixed", "duration_ms": 20}
        states.insert(0, {"name": "Lead", "timeout": {"after": "lead", "goto": "Answer"}})
        entry = "Lead"
    return {"name": name, "entry": entry, "distributions": distributions, "states": states}


# ------------------------------------------------------------------ the rig ---


def a_free_port() -> int:
    """A free port, for a daemon of this suite's own.

    There is a race between closing this socket and the child binding it, and
    it is the one every test harness accepts: the alternative is a fixed port,
    and a fixed port makes two runs of this suite on one machine collide —
    which is a certainty rather than a race.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@dataclass
class BenchRig:
    """One board, the daemon in front of it, and a client pointed at that.

    `native` is true when the far end is the firmware built for this machine,
    whose scan is a nanosleep on a preemptible kernel: everything but the
    timing budgets means the same thing there.
    """

    client: StatemachinedClient
    board: str
    native: bool
    board_input_pins: list[str]
    board_output_pins: list[str]
    scratch: Path
    _next_trial_id: int = 1000
    _loaded: tuple[str, ...] = field(default_factory=tuple)
    #: The set version the last `use` committed, as the daemon reported it.
    set_version: int = 0

    def line_map(self) -> dict:
        """Every line the board has, named `in<n>` and `out<n>` by index."""
        return {
            "input_lines": [
                {"name": f"in{index}", "pin_label": label, "debounce_milliseconds": 0}
                for index, label in enumerate(self.board_input_pins[:8])
            ],
            "output_lines": [
                {"name": f"out{index}", "pin_label": label}
                for index, label in enumerate(self.board_output_pins[:8])
            ],
        }

    def use(self, *graphs: dict) -> None:
        """Commit these graphs as the session's set, unless they already are.

        Through the documents a rig uses: a state machine config carrying the
        suite's line map and the graphs, loaded, and a session opened on it.
        """
        names = tuple(graph["name"] for graph in graphs)
        if names == self._loaded:
            return
        document = {
            "name": "hardware-suite",
            "description": "written by client/python/tests/hardware",
            "board": "",
            "line_map": self.line_map(),
            "graphs": list(graphs),
        }
        self.client.write_config("hardware-suite", json.dumps(document, indent=2))
        self.client.load_config("hardware-suite")
        self.set_version = self.client.open_session().set_version
        self._loaded = names

    def next_trial_id(self) -> int:
        self._next_trial_id += 1
        return self._next_trial_id

    def run(self, graph: str, *, cap_milliseconds: int = 5000) -> TrialResult:
        """Arm, start, wait, read. Every measurement here is one of these."""
        trial_id = self.next_trial_id()
        since = self.client.read_state().newest_trace_entry_number
        self.client.configure_trial(trial_id, graph=graph, cap_milliseconds=cap_milliseconds)
        self.client.start_trial(trial_id)
        self.client.wait_for_trial(
            trial_id, timeout_s=cap_milliseconds / 1000 + 5, since_entry_number=since
        )
        return self.client.read_trial_result(trial_id)


def start_native_device(scratch: Path) -> tuple[subprocess.Popen, str]:
    """The firmware built for this machine, with the loopback harness wired in."""
    binary = Path(os.environ.get("STATEMACHINED_NATIVE_DEVICE", BUILT_NATIVE_DEVICE))
    if not binary.is_file():
        raise FileNotFoundError(f"{binary} is not built; `make integration-device` builds it")
    process = subprocess.Popen(
        [str(binary), "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "STATEMACHINED_LOOPBACK": "8",
            "STATEMACHINED_STORE": str(scratch / "native-store.bin"),
        },
        text=True,
    )
    first_line = process.stdout.readline().strip() if process.stdout else ""
    if not first_line.startswith("listening on "):
        process.kill()
        raise RuntimeError(f"the native device said {first_line!r}")
    return process, "socket://" + first_line.removeprefix("listening on ")


def start_daemon(target: str, scratch: Path) -> tuple[subprocess.Popen, StatemachinedClient]:
    binary = Path(os.environ.get("STATEMACHINED_BINARY", BUILT_DAEMON))
    if not binary.is_file():
        raise FileNotFoundError(f"{binary} is not built; `cargo build` makes it")
    port = a_free_port()
    configuration = scratch / "rig.toml"
    configuration.write_text(
        "\n".join(
            [
                f'device_target = "{target}"',
                "connect_on_startup = true",
                'expected_board = ""',
                f'graph_store_directory = "{scratch / "graphs"}"',
                f'state_machine_config_directory = "{scratch / "configs"}"',
                f'trace_directory = "{scratch / "trace"}"',
                f'recording_directory = "{scratch / "recordings"}"',
                "",
            ]
        )
    )
    daemon = subprocess.Popen(
        [
            str(binary),
            "serve",
            "--rig-config",
            str(configuration),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-mdns",
        ],
        stdout=subprocess.DEVNULL,
        stderr=open(scratch / "daemon.log", "w"),  # noqa: SIM115 -- lives as long as the daemon
    )
    client = StatemachinedClient(f"127.0.0.1:{port}")
    client.wait_until_ready(timeout_s=30)
    return daemon, client


def scratch_directory() -> Path:
    return Path(tempfile.mkdtemp(prefix="statemachined-hardware-"))


def within_budget(rig: BenchRig, within: bool, finding: str) -> None:
    """A timing budget, asserted on silicon and only there.

    The host build's scan is a nanosleep on a preemptible kernel, honest to a
    millisecond or so, and a budget of a few hundred microseconds would be
    testing the kernel. So on the native device the measurement is still taken
    -- everything that leads up to it is exercised -- and the budget is not
    judged.
    """
    if rig.native:
        return
    assert within, finding


def wait_until(condition, *, timeout_s: float, poll_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(poll_s)
    return condition()
