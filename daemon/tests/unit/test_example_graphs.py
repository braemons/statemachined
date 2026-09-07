# SPDX-License-Identifier: GPL-3.0-or-later
"""The graphs this repository ships, checked the way a session would check them.

`graphs/` had been empty since M0 and the plan kept referring to it. These are
the two paradigms dev/PLAN.md names -- a go/no-go and a two-alternative forced
choice -- authored against the line map of the reference rig, and they are here
as much to be *read* as to be run: they are the worked example of what a graph
file is.

The test is the useful one: they load, they compile against a real board's
declared capacities, and they fit on it together, which is the question a
session actually asks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from statemachined.device.device_pin_map import DevicePinMap
from statemachined.graph_set_compiler import (
    DeviceCapabilities,
    compile_graph_set_for_device,
)
from statemachined.model.graph_definition import GraphDefinition
from statemachined.model.line_map import LineMap
from statemachined.model.state_machine_config import StateMachineConfig

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_GRAPH_DIRECTORY = REPOSITORY_ROOT / "graphs"
EXAMPLE_CONFIG_DIRECTORY = REPOSITORY_ROOT / "configs"
EXAMPLE_GRAPH_NAMES = ["go-nogo", "two-alternative-forced-choice"]

#: The Uno R4 Minima rig image, as `hello_ack` declares it. Written out rather
#: than imported from anywhere: if the firmware's capacities change, this is
#: where an example that no longer fits should fail.
UNO_R4_MINIMA_CAPABILITIES = DeviceCapabilities(
    max_line=512,
    max_states=32,
    max_transitions=64,
    max_output_actions=64,
    max_distributions=32,
    max_choice_options=32,
    max_path=255,
    max_graphs=20,
    input_line_count=8,
    output_line_count=8,
)


#: What the board answers to `pins` (dev/PROTOCOL.md §3.6), and the same table
#: `pinMode()` is called over in firmware/hal/renesas_ra4m1.cpp. Written out
#: here for the same reason the capacities above are: if the firmware's pinout
#: moves, this is where an example authored against the old one should fail.
UNO_R4_MINIMA_PINS = DevicePinMap(
    input_pin_labels=["D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9"],
    output_pin_labels=["D10", "D11", "D12", "A0", "A1", "A2", "A3", "A4"],
    source="device",
)


def load_example_state_machine_config() -> StateMachineConfig:
    """The shipped config: the reference rig's map, and both paradigms.

    This is the file `make bench` seeds the store with and comes up loaded
    with, so it is the one worth testing -- the graphs in `graphs/` are the
    library it was assembled out of.
    """
    path = EXAMPLE_CONFIG_DIRECTORY / "uno-r4-minima-bench.config.json"
    return StateMachineConfig.model_validate(json.loads(path.read_text()))


def load_example_line_map() -> LineMap:
    """The reference rig's map, numbered the way the daemon numbers it.

    The config names pins and no line numbers at all, which is the form a rig
    should be configured in -- so there is nothing to compile against until a
    board has been asked. Resolving here is not test scaffolding around that:
    it is the step `RigService.apply_state_machine_config` takes before it
    pushes a single mask, and doing it makes these tests exercise the path a
    session uses rather than a numbering no config carries any more.
    """
    return load_example_state_machine_config().line_map.resolved_against(UNO_R4_MINIMA_PINS)


def load_example_graph(graph_name: str) -> GraphDefinition:
    path = EXAMPLE_GRAPH_DIRECTORY / f"{graph_name}.json"
    return GraphDefinition.model_validate(json.loads(path.read_text()))


@pytest.mark.parametrize("graph_name", EXAMPLE_GRAPH_NAMES)
def test_an_example_graph_is_a_graph(graph_name: str):
    graph = load_example_graph(graph_name)
    assert graph.name == graph_name


def test_the_shipped_config_holds_the_graphs_it_names():
    """A config is self-contained: the graphs are in it, not fetched by name.

    Which is what makes one archivable beside a session's data and diffable
    against the config that was running the week the numbers changed. The
    copies in `graphs/` are the library it was assembled out of, and this
    checks the assembly did not drift from it.
    """
    config = load_example_state_machine_config()
    assert [graph.name for graph in config.graphs] == EXAMPLE_GRAPH_NAMES
    for graph in config.graphs:
        assert graph == load_example_graph(graph.name), (
            f"{graph.name} in the config has drifted from graphs/{graph.name}.json"
        )


def test_the_example_line_map_says_pins_and_not_line_numbers():
    """The map is written in what is silkscreened on the board.

    A bit position is written nowhere on the hardware, and it is the half of
    the pair that quietly stops being true when `kInputPins` is reordered. So
    the file carries pins alone, and this is what stops a helpful edit putting
    the numbers back.
    """
    as_written = load_example_state_machine_config().line_map
    for definition in [*as_written.input_lines, *as_written.output_lines]:
        assert definition.line_index is None, f"{definition.name} names a bit position"
        assert definition.pin_label, f"{definition.name} names no pin"


def test_the_example_line_map_is_a_line_map():
    line_map = load_example_line_map()
    # Resolved from "D6" by asking the board, which is the whole point of the
    # file naming a pin: 4 is a fact about firmware/hal/renesas_ra4m1.cpp, not
    # something the config was in a position to assert.
    assert line_map.input_line_index_for_name("lever_left") == 4
    # The valve is held open by a low, so its safe level is high. It is the
    # concrete case the whole wiring move exists for: a board that came up
    # driving every line low would open it on every power cycle.
    assert line_map.wiring_message_fields()["safe"] == 1 << 3


def test_both_examples_fit_the_reference_board_at_the_same_time():
    # The question a session asks: not "does this graph fit" but "do the graphs
    # this session will use fit *together*", because they share the pools.
    compiled = compile_graph_set_for_device(
        [load_example_graph(name) for name in EXAMPLE_GRAPH_NAMES],
        load_example_line_map(),
        UNO_R4_MINIMA_CAPABILITIES,
        set_version=1,
    )
    for pool_name, used in compiled.pool_usage.items():
        assert used <= compiled.pool_capacity[pool_name], pool_name


def test_the_examples_share_the_foreperiod_they_both_declare():
    # Both paradigms use the same truncated-exponential foreperiod, written the
    # same way, so the set spends one of the board's thirty-two distribution
    # entries on it rather than two.
    compiled = compile_graph_set_for_device(
        [load_example_graph(name) for name in EXAMPLE_GRAPH_NAMES],
        load_example_line_map(),
        UNO_R4_MINIMA_CAPABILITIES,
        set_version=1,
    )
    go_nogo, two_choice = compiled.graphs_by_slot
    assert (
        go_nogo.distribution_pool_index_by_name["foreperiod"]
        == two_choice.distribution_pool_index_by_name["foreperiod"]
    )


def test_a_reward_is_a_pulse_the_device_serves_itself():
    # The timing authority is down there, which is the whole architecture in one
    # field: the host says "40 ms" once and never has to lower the line.
    graph = load_example_graph("go-nogo")
    hit = graph.state_named("Hit")
    reward = next(action for action in hit.on_entry if action.line == "reward_valve")
    assert reward.kind == "pulse"
    assert reward.pulse_ms == 40
