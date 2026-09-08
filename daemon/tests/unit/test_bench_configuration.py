# SPDX-License-Identifier: GPL-3.0-or-later
"""The bench's two configs are real config, and they have to load.

`make bench` is how anybody sees the web UI in front of a device, so the files
it reads have to work -- a typo there is discovered by the one person least able
to debug it, on the day they first try the thing.

Two files now, and the split is the point of these tests as much as the loading
is: the rig config says what the box is and holds no line map at all, and the
state-machine config holds the map and the graphs. A line map that crept back
into the rig config would be a conffile the daemon writes, which is a file that
fights dpkg on every upgrade.
"""

from __future__ import annotations

import json
from pathlib import Path

from statemachined.model.state_machine_config import StateMachineConfig
from statemachined.rig_configuration import RigConfiguration

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BENCH_RIG_CONFIG = (
    REPOSITORY_ROOT / "daemon" / "bench" / "statemachined_bench_rig_config.toml"
)
BENCH_STATE_MACHINE_CONFIG = (
    REPOSITORY_ROOT / "configs" / "uno-r4-minima-bench.config.json"
)


def load_the_bench_rig_config() -> RigConfiguration:
    return RigConfiguration.load_from_toml_file(BENCH_RIG_CONFIG)


def load_the_bench_state_machine_config() -> StateMachineConfig:
    return StateMachineConfig.model_validate(json.loads(BENCH_STATE_MACHINE_CONFIG.read_text()))


def test_the_bench_rig_config_loads() -> None:
    configuration = load_the_bench_rig_config()
    assert configuration.device_target
    # And nothing about anybody else. This daemon reports to nobody and has no
    # setting naming another daemon: it publishes to its trace and whoever wants
    # it opens a stream. A bench box is therefore not a special case.
    assert not any("triald" in name for name in vars(configuration))


def test_the_bench_writes_only_under_the_checkout() -> None:
    """build/ is git-ignored, and /var/lib is the package's. A bench that wrote
    into a rig's directories would be a bench nobody could run on a rig."""
    configuration = load_the_bench_rig_config()
    for directory in (
        configuration.graph_store_directory,
        configuration.state_machine_config_directory,
        configuration.trace_directory,
        configuration.recording_directory,
    ):
        assert not directory.is_absolute(), f"{directory} would escape the checkout"
        assert directory.parts[0] == "build"


def test_the_bench_comes_up_loaded_with_a_config_that_exists() -> None:
    """The named startup config is the one this repository ships.

    `make bench` seeds the store from `configs/`, so a rig config naming a
    config that is not there would come up wired to nothing -- and the symptom
    is a Lines panel with no lines, which reads like a broken daemon rather than
    a mistyped name.
    """
    configuration = load_the_bench_rig_config()
    assert configuration.startup_state_machine_config
    shipped = {path.name[: -len(".config.json")] for path in (REPOSITORY_ROOT / "configs").glob("*.config.json")}
    assert configuration.startup_state_machine_config in shipped


def test_the_rig_config_holds_no_line_map() -> None:
    """The split, asserted where it would be undone.

    `RigConfiguration` forbids members it does not declare, so a `[line_map]`
    put back into the bench conffile fails at load -- but a *reader* of this
    test is the point: the map belongs in the state-machine config, because
    that is the file the daemon may write.
    """
    assert not hasattr(load_the_bench_rig_config(), "line_map")
    assert "line_map" not in RigConfiguration.model_fields


def test_the_bench_state_machine_config_is_the_reference_boards() -> None:
    config = load_the_bench_state_machine_config()
    assert config.board == "uno_r4_minima"
    assert [graph.name for graph in config.graphs] == [
        "go-nogo",
        "two-alternative-forced-choice",
        # Not a paradigm: the bench instrument, which is here because the first
        # question at a bench is whether the rig moves at all, and answering it
        # with a paradigm means reading a trace to find out that nothing was
        # wrong with the rig.
        "state-walk",
    ]
    assert config.line_map.input_lines and config.line_map.output_lines


def test_the_bench_line_map_names_pins_rather_than_line_numbers() -> None:
    """Which pin is silkscreened on the board; which bit position is not.

    So the file says the half a person at the bench can check, and the daemon
    asks the board for the other half at load (dev/PROTOCOL.md 3.6). A
    `line_index` here would also be the half that stops being true the moment
    `kInputPins` is reordered, with nothing raised anywhere -- and now that a
    config is self-contained and therefore portable, it is the half that would
    travel to another rig and quietly mean a different hole.
    """
    line_map = load_the_bench_state_machine_config().line_map
    lines = [*line_map.input_lines, *line_map.output_lines]
    assert lines
    for definition in lines:
        assert definition.line_index is None, f"{definition.name} names a bit position"
        assert definition.pin_label, f"{definition.name} names no pin"
