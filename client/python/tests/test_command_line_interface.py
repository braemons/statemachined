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
