# SPDX-License-Identifier: LGPL-3.0-or-later
"""`statemachinectl` — one rig's state machine, from a terminal.

Named without the `d`: the daemon is `statemachined` and it installs a program
of that name on every rig this would also be installed on. Two different
programs answering to one word is a bug report about the wrong one.

**This is a view onto the client and holds no logic of its own.** Anything it
can work out, :class:`~statemachined_client.daemon_client.StatemachinedClient`
could have; anything it decided would be a second opinion about a session that
already has one.

It follows the family's rules for a `<name>ctl` (`contracts/DAEMON_LAYOUT.md`):
`--rig`, then `$BRAEMONS_RIG`, then localhost; JSON on stdout, one compact
object per line for a stream; a refusal as one JSON object on stderr, with an
exit status a script can switch on and a sentence a person can read.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum, IntEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .api_types import RigConfigurationPatch
from .daemon_client import DEFAULT_PORT, StatemachinedClient
from .daemon_refusals import DaemonRefusedTheRequest

RIG_ENVIRONMENT_VARIABLE = "BRAEMONS_RIG"


class ExitStatus(IntEnum):
    """The same numbers from every `<name>ctl` in the family."""

    OK = 0
    FAILURE = 1
    USAGE = 2
    UNAVAILABLE = 3
    TIMED_OUT = 4
    REFUSED = 5
    NOT_FOUND = 6
    INTERRUPTED = 130


_EXIT_STATUS_FOR_REFUSAL = {
    "unavailable": ExitStatus.UNAVAILABLE,
    "deadline_exceeded": ExitStatus.TIMED_OUT,
    "not_found": ExitStatus.NOT_FOUND,
}


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


def _print_line(value) -> None:
    """One compact object per line, flushed: this is what somebody leaves
    running in a second terminal, and an entry that arrives four kilobytes
    late is not an entry."""
    print(json.dumps(_plain(value)), flush=True)


def _fail(error: str, detail: str, exit_status: ExitStatus, **more) -> int:
    print(json.dumps({"error": error, "detail": detail, **more}), file=sys.stderr)
    return exit_status


def _client_version() -> str:
    try:
        return version("statemachined-client")
    except PackageNotFoundError:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="statemachinectl",
        description="Talk to a statemachined. Everything prints JSON, so it pipes into jq.",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"statemachinectl {_client_version()}"
    )
    parser.add_argument(
        "--rig",
        default=os.environ.get(RIG_ENVIRONMENT_VARIABLE) or "localhost",
        help=f"host, or host:port (default ${RIG_ENVIRONMENT_VARIABLE}, then localhost; "
        f"port {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="how long to wait for the daemon to answer (default %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("health", help="is the daemon up, and does it have a board")
    commands.add_parser("state", help="everything true right now")
    commands.add_parser("device", help="the board, as the daemon last saw it")
    lines = commands.add_parser(
        "lines", help="the wiring: named lines and the board's own pins"
    )
    line_actions = lines.add_subparsers(dest="action")
    lines_put = line_actions.add_parser(
        "put", help="write a line map, push it to the board and save it into the config"
    )
    lines_put.add_argument("file", help="the line map as JSON, or - for stdin")
    lines_put.add_argument("--name", help="default: the file's stem")
    commands.add_parser("firmware", help="what the board runs against what this host has")
    commands.add_parser("session", help="the session and what is loaded")
    commands.add_parser("observers", help="who is watching this rig right now")
    rig_config = commands.add_parser("rig-config", help="the rig config: this box's hardware")
    rig_config_actions = rig_config.add_subparsers(dest="action")
    rig_config_set = rig_config_actions.add_parser(
        "set", help="change some of it until the daemon restarts; /etc is never written"
    )
    rig_config_set.add_argument("--device-target")
    rig_config_set.add_argument("--device-baud", type=int)
    rig_config_set.add_argument("--expected-board")
    rig_config_set.add_argument("--graph-mode")
    rig_config_set.add_argument("--startup-state-machine-config")
    rig_config_set.add_argument("--session-seed")
    commands.add_parser("link", help="open the serial link, or reopen it")
    commands.add_parser(
        "save-to-board", help="write the board's wiring, graph set and autorun to its flash"
    )

    autorun = commands.add_parser("autorun", help="the board's own trial loop, with no host")
    autorun_actions = autorun.add_subparsers(dest="action")
    autorun_on = autorun_actions.add_parser("on", help="turn it on")
    autorun_on.add_argument("--graph", dest="graph_name", help="default: the one it had")
    autorun_on.add_argument("--cap-ms", type=int, default=0, dest="cap_milliseconds")
    autorun_on.add_argument("--seed", type=int, help="fix the draw; default: the board picks")
    autorun_on.add_argument("--first-trial-id", type=int)
    autorun_on.add_argument(
        "--start-now",
        action=argparse.BooleanOptionalAction,
        help="--no-start-now to enable it for the next power-up only, so it can be saved",
    )
    autorun_actions.add_parser("off", help="turn it off")

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
    graph_put.add_argument("file", help="the file to store, or - for stdin")
    graph_put.add_argument("--name", help="default: the file's stem")
    graph_remove = graph_actions.add_parser("rm", help="delete one graph")
    graph_remove.add_argument("name")
    graph_check = graph_actions.add_parser("check", help="compile it against the board")
    graph_check.add_argument(
        "name", nargs="?", help="a stored graph; omit and pass --file for a draft"
    )
    graph_check.add_argument("--file", dest="draft", help="a graph to check without storing it")
    graph_upload = graph_actions.add_parser(
        "upload", help="push one stored graph to the board, replacing the committed set"
    )
    graph_upload.add_argument("name")

    configs = commands.add_parser("configs", help="the state machine config store")
    config_actions = configs.add_subparsers(dest="action", required=True)
    config_actions.add_parser("list", help="every config, and which is loaded")
    config_get = config_actions.add_parser("get", help="print one config's text")
    config_get.add_argument("name")
    config_put = config_actions.add_parser("put", help="write a .json into the store")
    config_put.add_argument("file", help="the file to store, or - for stdin")
    config_put.add_argument("--name", help="default: the file's stem")
    config_remove = config_actions.add_parser("rm", help="delete one config")
    config_remove.add_argument("name")
    config_load = config_actions.add_parser("load", help="load one, and push its wiring")
    config_load.add_argument("name")

    session = commands.add_parser("open", help="commit the loaded config's graph set")
    session.add_argument(
        "graphs", nargs="*", help="graphs to commit instead of the config's own"
    )
    commands.add_parser("close", help="end the session")
    active_graph = commands.add_parser(
        "active-graph", help="the graph a trial configured without one uses"
    )
    active_graph_actions = active_graph.add_subparsers(dest="action", required=True)
    active_graph_set = active_graph_actions.add_parser("set", help="choose it")
    active_graph_set.add_argument("graph")
    active_graph_actions.add_parser("clear", help="unset it: every trial names its graph")

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

    recording = commands.add_parser("recordings", help="take a named recording off the ring")
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
    recording_entries = recording_actions.add_parser("entries", help="a page of its entries")
    recording_entries.add_argument("name")
    recording_entries.add_argument("--offset", type=int, default=0)
    recording_entries.add_argument("--limit", type=int, default=500)
    recording_remove = recording_actions.add_parser("rm", help="delete a closed recording")
    recording_remove.add_argument("name")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if getattr(arguments, "file", None) == "-" and not arguments.name:
        parser.error("put - reads stdin and needs --name")
    checking = arguments.command == "graphs" and arguments.action == "check"
    if checking and not (arguments.name or arguments.draft):
        parser.error("check needs a stored graph's name, or --file")

    try:
        with StatemachinedClient(arguments.rig) as rig:
            # Before anything else, so that "nothing is listening" is one clear
            # sentence rather than whatever gRPC says about HTTP/2 frames.
            rig.wait_until_ready(timeout_s=arguments.timeout)
            return _run(rig, arguments)
    except DaemonRefusedTheRequest as refusal:
        # To stderr, and JSON, so that a script can read the refusal and a
        # person can read the sentence. `error` is what a script switches on
        # and `context` is what to change.
        return _fail(
            refusal.error,
            refusal.detail,
            _EXIT_STATUS_FOR_REFUSAL.get(refusal.status, ExitStatus.REFUSED),
            context=refusal.context,
            status=refusal.status,
        )
    except TimeoutError as problem:
        return _fail("unavailable", str(problem), ExitStatus.UNAVAILABLE)
    except OSError as problem:
        return _fail("unreadable", str(problem), ExitStatus.FAILURE)
    except (KeyboardInterrupt, BrokenPipeError):
        return ExitStatus.INTERRUPTED


def _run(rig: StatemachinedClient, arguments) -> int:
    match arguments.command:
        case "health":
            _print(rig.read_health())
        case "state":
            _print(rig.read_state())
        case "device":
            _print(rig.read_device())
        case "lines" if arguments.action == "put":
            _print(rig.write_line_map(*_named_document(arguments)))
        case "lines":
            _print(rig.read_lines())
        case "firmware":
            _print(rig.read_firmware())
        case "session":
            _print(rig.read_session())
        case "observers":
            _print(rig.read_observers())
        case "rig-config" if arguments.action == "set":
            _print(
                rig.patch_configuration(
                    RigConfigurationPatch(
                        device_target=arguments.device_target,
                        device_baud=arguments.device_baud,
                        expected_board=arguments.expected_board,
                        graph_mode=arguments.graph_mode,
                        startup_state_machine_config=arguments.startup_state_machine_config,
                        session_seed=arguments.session_seed,
                    )
                )
            )
        case "rig-config":
            _print(rig.read_configuration())
        case "link":
            _print(rig.open_link())
        case "save-to-board":
            _print(rig.save_settings())
        case "autorun":
            _autorun(rig, arguments)
        case "active-graph":
            _print(
                {
                    "active_graph": rig.set_active_graph(arguments.graph)
                    if arguments.action == "set"
                    else rig.clear_active_graph()
                }
            )
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
        case "recordings":
            return _recording(rig, arguments)
    return ExitStatus.OK


def _trace(rig: StatemachinedClient, arguments) -> int:
    if arguments.trial is not None:
        _print(rig.read_trial_trace(arguments.trial))
        return 0
    if not arguments.follow:
        _print(rig.read_trace(arguments.since, arguments.limit))
        return 0
    with rig.watch_trace(arguments.since) as entries:
        for entry in entries:
            _print_line(entry)
    return 0


def _monitor(rig: StatemachinedClient, arguments) -> int:
    if not arguments.follow:
        _print(rig.read_serial_monitor(arguments.since, arguments.limit))
        return 0
    with rig.watch_serial_monitor(arguments.since) as entries:
        for entry in entries:
            _print_line(entry)
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
            # The file's own stem is the name it is filed under unless --name
            # says otherwise, and the daemon refuses it if the document
            # disagrees — which is the check, rather than this guessing which
            # of the two was meant.
            _print(rig.write_graph(*_named_document(arguments)))
        case "rm":
            _print(rig.delete_graph(arguments.name))
        case "check":
            validation = (
                rig.validate_graph_text(_read(arguments.draft))
                if arguments.draft
                else rig.validate_graph(arguments.name)
            )
            _print(validation)
            return ExitStatus.OK if validation.valid else ExitStatus.REFUSED
        case "upload":
            _print(rig.upload_graph(arguments.name))
    return 0


def _configs(rig: StatemachinedClient, arguments) -> int:
    match arguments.action:
        case "list":
            _print(rig.list_configs())
        case "get":
            print(rig.read_config(arguments.name).text, end="")
        case "put":
            _print(rig.write_config(*_named_document(arguments)))
        case "rm":
            _print(rig.delete_config(arguments.name))
        case "load":
            _print(rig.load_config(arguments.name))
    return 0


def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def _autorun(rig: StatemachinedClient, arguments) -> None:
    match arguments.action:
        case None:
            _print(rig.read_autorun())
        case "on":
            _print(
                rig.write_autorun(
                    True,
                    graph_name=arguments.graph_name,
                    cap_milliseconds=arguments.cap_milliseconds,
                    seed=arguments.seed,
                    first_trial_id=arguments.first_trial_id,
                    start_now=arguments.start_now,
                )
            )
        case "off":
            _print(rig.write_autorun(False))


def _named_document(arguments) -> tuple[str, str]:
    """`put FILE [--name NAME]`: the name, and the text as it is on disk."""
    if arguments.file == "-":
        return arguments.name, sys.stdin.read()
    path = Path(arguments.file)
    return arguments.name or path.stem, path.read_text(encoding="utf-8")


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
        case "entries":
            _print(
                rig.read_recording_entries(arguments.name, arguments.offset, arguments.limit)
            )
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
                _print_line(state)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
