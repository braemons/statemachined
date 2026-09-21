# SPDX-License-Identifier: LGPL-3.0-or-later
"""`statemachinectl` — one rig's state machine, from a terminal.

Named without the `d`: the daemon is `statemachined` and it installs a program
of that name on every rig this would also be installed on. Two different
programs answering to one word is a bug report about the wrong one.

**This is a view onto the client and holds no logic of its own.** Anything it
can work out, :class:`~statemachined_client.daemon_client.StatemachinedClient`
could have; anything it decided would be a second opinion about a session that
already has one.

Everything prints JSON, so it pipes into `jq`. A refusal prints as JSON too —
on stderr, with a non-zero exit — so a shell script can read which refusal it
was and a person can read the sentence.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from .daemon_client import DEFAULT_PORT, StatemachinedClient
from .daemon_refusals import DaemonRefusedTheRequest


def _plain(value):
    """A dataclass tree as JSON.

    Enums print as their short name — the one this client speaks — except
    `TrialOutcome`, which is an `IntEnum` and prints as the `.tdr` code, since
    that is the number every analysis script already reads.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {name: _plain(field) for name, field in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _print(value) -> None:
    print(json.dumps(_plain(value), indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="statemachinectl",
        description="Talk to a statemachined. Everything prints JSON, so it pipes into jq.",
    )
    parser.add_argument(
        "--rig",
        default="localhost",
        help=f"host, or host:port (default {DEFAULT_PORT}, one above the browser's)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("health", help="is the daemon up, and does it have a board")
    commands.add_parser("state", help="everything true right now")
    commands.add_parser("device", help="the board, as the daemon last saw it")
    commands.add_parser("lines", help="the wiring: named lines and the board's own pins")
    commands.add_parser("firmware", help="what the board runs against what this host has")
    commands.add_parser("session", help="the session and what is loaded")
    commands.add_parser("observers", help="who is watching this rig right now")
    commands.add_parser("rig-config", help="the rig config: this box's hardware")
    commands.add_parser("link", help="open the serial link, or reopen it")

    watch = commands.add_parser("watch", help="follow the state stream until interrupted")
    watch.add_argument(
        "--summary", action="store_true", help="one line per frame instead of the whole state"
    )

    trace = commands.add_parser("trace", help="the trace ring")
    trace.add_argument("--since", type=int, default=0, help="entry number to start from")
    trace.add_argument("--limit", type=int, default=500)
    trace.add_argument(
        "--follow", action="store_true", help="keep printing entries as they arrive"
    )
    trace.add_argument("--trial", type=int, help="only this trial's entries, from the ring")

    monitor = commands.add_parser("monitor", help="lines of text on the serial port")
    monitor.add_argument("--since", type=int, default=0)
    monitor.add_argument("--limit", type=int, default=500)
    monitor.add_argument("--follow", action="store_true")

    graphs = commands.add_parser("graphs", help="the graph store")
    graph_actions = graphs.add_subparsers(dest="action", required=True)
    graph_actions.add_parser("list", help="every graph, and whether it reads")
    graph_get = graph_actions.add_parser("get", help="print one graph's text")
    graph_get.add_argument("name")
    graph_put = graph_actions.add_parser("put", help="write a .json into the store")
    graph_put.add_argument("path", type=Path)
    graph_remove = graph_actions.add_parser("rm", help="delete one graph")
    graph_remove.add_argument("name")
    graph_check = graph_actions.add_parser("check", help="compile it against the board")
    graph_check.add_argument("name")

    configs = commands.add_parser("configs", help="the state machine config store")
    config_actions = configs.add_subparsers(dest="action", required=True)
    config_actions.add_parser("list", help="every config, and which is loaded")
    config_get = config_actions.add_parser("get", help="print one config's text")
    config_get.add_argument("name")
    config_put = config_actions.add_parser("put", help="write a .json into the store")
    config_put.add_argument("path", type=Path)
    config_remove = config_actions.add_parser("rm", help="delete one config")
    config_remove.add_argument("name")
    config_load = config_actions.add_parser("load", help="load one, and push its wiring")
    config_load.add_argument("name")

    session = commands.add_parser("open", help="commit the loaded config's graph set")
    session.add_argument(
        "graphs", nargs="*", help="graphs to commit instead of the config's own"
    )
    commands.add_parser("close", help="end the session")

    trial = commands.add_parser("trial", help="arm, start, cancel or read a trial")
    trial_actions = trial.add_subparsers(dest="action", required=True)
    arm = trial_actions.add_parser("arm", help="configure one trial on the board")
    arm.add_argument("trial_id", type=int)
    arm.add_argument("--graph", default="", help="default: the session's active graph")
    arm.add_argument("--cap-ms", type=int, default=0, dest="cap_milliseconds")
    start = trial_actions.add_parser("start", help="start the armed trial")
    start.add_argument("trial_id", type=int)
    cancel = trial_actions.add_parser("cancel", help="stop a trial in flight")
    cancel.add_argument("trial_id", type=int)
    result = trial_actions.add_parser("result", help="a finished trial, with its path")
    result.add_argument("trial_id", nargs="?", type=int)

    recording = commands.add_parser("recording", help="take a named recording off the ring")
    recording_actions = recording.add_subparsers(dest="action", required=True)
    recording_actions.add_parser("list", help="the store, and the one being written to")
    recording_start = recording_actions.add_parser("start", help="begin keeping entries")
    recording_start.add_argument("name", nargs="?", default="")
    recording_start.add_argument("--description", default="")
    recording_actions.add_parser("pause", help="stop keeping entries, without closing")
    recording_actions.add_parser("resume", help="start a new segment")
    recording_actions.add_parser("stop", help="close it and write it out")
    recording_actions.add_parser("clear", help="throw the open one away")
    recording_get = recording_actions.add_parser("get", help="one recording's manifest")
    recording_get.add_argument("name")
    recording_remove = recording_actions.add_parser("rm", help="delete a closed recording")
    recording_remove.add_argument("name")

    arguments = parser.parse_args(argv)

    try:
        with StatemachinedClient(arguments.rig) as rig:
            # Before anything else, so that "nothing is listening" and "that is
            # the browser's port" are one clear sentence rather than whatever
            # gRPC says about HTTP/2 frames.
            rig.wait_until_ready(timeout_s=5)
            return _run(rig, arguments)
    except DaemonRefusedTheRequest as refusal:
        # To stderr, and JSON, so that a script can read the refusal and a
        # person can read the sentence. `error` is what a script switches on
        # and `context` is what to change.
        print(
            json.dumps(
                {
                    "error": refusal.error,
                    "detail": refusal.detail,
                    "context": refusal.context,
                    "status": refusal.status,
                }
            ),
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        return 130


def _run(rig: StatemachinedClient, arguments) -> int:
    match arguments.command:
        case "health":
            _print(rig.read_health())
        case "state":
            _print(rig.read_state())
        case "device":
            _print(rig.read_device())
        case "lines":
            _print(rig.read_lines())
        case "firmware":
            _print(rig.read_firmware())
        case "session":
            _print(rig.read_session())
        case "observers":
            _print(rig.read_observers())
        case "rig-config":
            _print(rig.read_configuration())
        case "link":
            _print(rig.open_link())
        case "watch":
            _watch(rig, summary=arguments.summary)
        case "trace":
            return _trace(rig, arguments)
        case "monitor":
            return _monitor(rig, arguments)
        case "graphs":
            return _graphs(rig, arguments)
        case "configs":
            return _configs(rig, arguments)
        case "open":
            _print(
                rig.upload_graph_set(arguments.graphs)
                if arguments.graphs
                else rig.open_session()
            )
        case "close":
            _print(rig.close_session())
        case "trial":
            return _trial(rig, arguments)
        case "recording":
            return _recording(rig, arguments)
    return 0


def _trace(rig: StatemachinedClient, arguments) -> int:
    if arguments.trial is not None:
        _print(rig.read_trial_trace(arguments.trial))
        return 0
    if not arguments.follow:
        _print(rig.read_trace(arguments.since, arguments.limit))
        return 0
    with rig.watch_trace(arguments.since) as entries:
        for entry in entries:
            _print(entry)
            sys.stdout.flush()
    return 0


def _monitor(rig: StatemachinedClient, arguments) -> int:
    if not arguments.follow:
        _print(rig.read_serial_monitor(arguments.since, arguments.limit))
        return 0
    with rig.watch_serial_monitor(arguments.since) as entries:
        for entry in entries:
            print(f"{entry.direction} {entry.line}", flush=True)
    return 0


def _graphs(rig: StatemachinedClient, arguments) -> int:
    match arguments.action:
        case "list":
            _print(rig.list_graphs())
        case "get":
            # The text, not a parse of it: this pipes into a file, and a
            # reformatted graph is a diff nobody asked for.
            print(rig.read_graph(arguments.name).text, end="")
        case "put":
            # The file's own stem is the name it is filed under, and the daemon
            # refuses it if the document disagrees — which is the check, rather
            # than this guessing which of the two was meant.
            _print(rig.write_graph(arguments.path.stem, arguments.path.read_text()))
        case "rm":
            _print(rig.delete_graph(arguments.name))
        case "check":
            validation = rig.validate_graph(arguments.name)
            _print(validation)
            return 0 if validation.valid else 1
    return 0


def _configs(rig: StatemachinedClient, arguments) -> int:
    match arguments.action:
        case "list":
            _print(rig.list_configs())
        case "get":
            print(rig.read_config(arguments.name).text, end="")
        case "put":
            _print(rig.write_config(arguments.path.stem, arguments.path.read_text()))
        case "rm":
            _print(rig.delete_config(arguments.name))
        case "load":
            _print(rig.load_config(arguments.name))
    return 0


def _trial(rig: StatemachinedClient, arguments) -> int:
    match arguments.action:
        case "arm":
            _print(
                rig.configure_trial(
                    arguments.trial_id,
                    graph=arguments.graph,
                    cap_milliseconds=arguments.cap_milliseconds,
                )
            )
        case "start":
            _print(rig.start_trial(arguments.trial_id))
        case "cancel":
            _print(rig.cancel_trial(arguments.trial_id))
        case "result":
            _print(rig.read_trial_result(arguments.trial_id))
    return 0


def _recording(rig: StatemachinedClient, arguments) -> int:
    match arguments.action:
        case "list":
            _print(rig.read_recordings())
        case "start":
            _print(rig.start_recording(arguments.name, arguments.description))
        case "pause":
            _print(rig.pause_recording())
        case "resume":
            _print(rig.resume_recording())
        case "stop":
            _print(rig.stop_recording())
        case "clear":
            _print(rig.clear_recording())
        case "get":
            _print(rig.read_recording(arguments.name))
        case "rm":
            _print(rig.delete_recording(arguments.name))
    return 0


def _watch(rig: StatemachinedClient, *, summary: bool) -> None:
    """Follow the stream until Ctrl-C.

    Line-buffered on purpose: this is what somebody leaves running in a second
    terminal, and a state that arrives four kilobytes late is not a state.
    """
    with rig.watch_state() as states:
        for state in states:
            if summary:
                print(
                    f"{'connected' if state.connected else 'no board':10} "
                    f"{'running' if state.running else 'idle':8} "
                    f"trial {state.trial_id if state.trial_id is not None else '-':>8} "
                    f"{state.graph:16} {state.state_name or '-'}",
                    flush=True,
                )
            else:
                _print(state)
                sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
