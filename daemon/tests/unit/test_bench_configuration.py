# SPDX-License-Identifier: GPL-3.0-or-later
"""The bench config is real config, and it says what the board is wired like.

`make bench` is how anybody sees the web UI in front of a device, so the file it
reads has to load -- a typo there is discovered by the one person least able to
debug it, on the day they first try the thing. And its line map has to be the
board's: two line maps for one reference board that disagree is a wire in the
wrong hole, found by a lever that does nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from statemachined.daemon_configuration import DaemonConfiguration
from statemachined.model.line_map import LineMap

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BENCH_CONFIGURATION = REPOSITORY_ROOT / "daemon" / "bench" / "statemachined_bench_configuration.toml"
REFERENCE_BOARD_LINES = REPOSITORY_ROOT / "graphs" / "uno-r4-minima-lines.json"


def test_the_bench_configuration_loads() -> None:
    configuration = DaemonConfiguration.load_from_toml_file(BENCH_CONFIGURATION)
    assert configuration.device_target
    # Nowhere to report to: a bench box has no triald, and a daemon that could
    # not run without one would be untestable exactly here.
    assert configuration.triald_base_url == ""


def test_the_bench_writes_only_under_the_checkout() -> None:
    """build/ is git-ignored, and /var/lib is the package's. A bench that wrote
    into a rig's directories would be a bench nobody could run on a rig."""
    configuration = DaemonConfiguration.load_from_toml_file(BENCH_CONFIGURATION)
    for directory in (configuration.graph_store_directory, configuration.trace_directory):
        assert not directory.is_absolute(), f"{directory} would escape the checkout"
        assert directory.parts[0] == "build"


def test_the_bench_line_map_is_the_reference_boards() -> None:
    configuration = DaemonConfiguration.load_from_toml_file(BENCH_CONFIGURATION)
    on_the_board = LineMap.model_validate(json.loads(REFERENCE_BOARD_LINES.read_text()))
    assert configuration.line_map == on_the_board
