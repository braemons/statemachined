# SPDX-License-Identifier: LGPL-3.0-or-later
"""`statemachinectl` keeps the family's rules for a `<name>ctl`.

`contracts/DAEMON_LAYOUT.md` lists them. These are the ones that need no
daemon: which rig, the version, and how a failure reads to a script.
"""

from __future__ import annotations

import json
import socket

import pytest
from statemachined_client.command_line_interface import ExitStatus, build_parser, main


def a_port_nobody_listens_on() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_rig_is_the_flag_then_the_environment_then_localhost(monkeypatch):
    monkeypatch.delenv("BRAEMONS_RIG", raising=False)
    assert build_parser().parse_args(["state"]).rig == "localhost"
    monkeypatch.setenv("BRAEMONS_RIG", "rig-a.local")
    assert build_parser().parse_args(["state"]).rig == "rig-a.local"
    assert build_parser().parse_args(["--rig", "rig-b", "state"]).rig == "rig-b"


def test_the_version_is_the_commands_own(capsys):
    with pytest.raises(SystemExit) as exit:
        main(["--version"])
    assert exit.value.code == 0
    assert capsys.readouterr().out.startswith("statemachinectl ")


def test_nothing_answering_is_unavailable_as_json_on_stderr(capsys):
    port = a_port_nobody_listens_on()
    status = main(["--rig", f"127.0.0.1:{port}", "--timeout", "0.2", "state"])
    assert status == ExitStatus.UNAVAILABLE == 3
    failure = json.loads(capsys.readouterr().err)
    assert failure["error"] == "unavailable"
    assert failure["detail"]


def test_a_document_from_stdin_needs_a_name():
    with pytest.raises(SystemExit) as exit:
        main(["graphs", "put", "-"])
    assert exit.value.code == ExitStatus.USAGE == 2


def test_a_line_map_from_stdin_needs_a_name_too():
    with pytest.raises(SystemExit) as exit:
        main(["lines", "put", "-"])
    assert exit.value.code == ExitStatus.USAGE


def test_a_graph_check_needs_a_name_or_a_draft():
    with pytest.raises(SystemExit) as exit:
        main(["graphs", "check"])
    assert exit.value.code == ExitStatus.USAGE
    assert build_parser().parse_args(["graphs", "check", "--file", "g.json"]).draft == "g.json"


def test_autorun_leaves_unnamed_settings_alone():
    """`None` is "leave it" in the client, so the CLI must not default them."""
    arguments = build_parser().parse_args(["autorun", "on", "--no-start-now"])
    assert (arguments.graph_name, arguments.seed, arguments.start_now) == (None, None, False)
    assert build_parser().parse_args(["autorun"]).action is None


def test_the_bare_reads_stay_reads():
    for command in ("lines", "rig-config", "autorun"):
        assert build_parser().parse_args([command]).action is None


# -- against a daemon ------------------------------------------------------------


def test_the_rig_config_is_patched_one_field_at_a_time(rig, capsys):
    before = rig.read_configuration()
    status = main(
        ["--rig", rig.address, "rig-config", "set", "--expected-board", "a-test-board"]
    )
    try:
        assert status == ExitStatus.OK
        update = json.loads(capsys.readouterr().out)
        assert update["configuration"]["expected_board"] == "a-test-board"
        assert update["configuration"]["graph_store_directory"] == before.graph_store_directory
    finally:
        main(
            [
                "--rig",
                rig.address,
                "rig-config",
                "set",
                "--expected-board",
                before.expected_board,
            ]
        )
