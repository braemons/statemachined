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

from statemachined.graph_set_compiler import DeviceCapabilities, compile_graph_set_for_device
from statemachined.model.graph_definition import GraphDefinition
from statemachined.model.line_map import LineMap

EXAMPLE_GRAPH_DIRECTORY = Path(__file__).resolve().parents[3] / "graphs"
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


def load_example_line_map() -> LineMap:
    path = EXAMPLE_GRAPH_DIRECTORY / "uno-r4-minima-lines.json"
    return LineMap.model_validate(json.loads(path.read_text()))


def load_example_graph(graph_name: str) -> GraphDefinition:
    path = EXAMPLE_GRAPH_DIRECTORY / f"{graph_name}.json"
    return GraphDefinition.model_validate(json.loads(path.read_text()))


@pytest.mark.parametrize("graph_name", EXAMPLE_GRAPH_NAMES)
def test_an_example_graph_is_a_graph(graph_name: str):
    graph = load_example_graph(graph_name)
    assert graph.name == graph_name


def test_the_example_line_map_is_a_line_map():
    line_map = load_example_line_map()
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
