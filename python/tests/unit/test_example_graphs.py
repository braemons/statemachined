# SPDX-License-Identifier: GPL-3.0-or-later
"""The graphs this repository ships, checked the way a session would check them.

`graphs/` had been empty since M0 and the plan kept referring to it. Two of
these are the paradigms dev/PLAN.md names -- a go/no-go and a two-alternative
forced choice -- authored against the line map of the reference rig, and they
are here as much to be *read* as to be run: they are the worked example of what
a graph file is. The third is `state-walk`, which is not a paradigm at all: it
is the bench instrument, a deterministic march through its own states at a
fixed 500 ms so that a person can watch the lamps and read the same march back
out of the trace.

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
EXAMPLE_GRAPH_NAMES = ["go-nogo", "two-alternative-forced-choice", "state-walk"]

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


#: What the board answers to `pins` (docs/reference/protocol.md §3.6), and the same table
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
    go_nogo, two_choice = compiled.graphs_by_slot[:2]
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


# ------------------------------------------------------------- the walk ---


def test_the_walk_is_deterministic_once_it_has_started():
    """The one property `state-walk` exists to have.

    It is the graph somebody runs to answer "is this rig doing anything at
    all", and the answer is only useful if the march is the same every time:
    six states, 500 ms each, in one order. So every state after the trigger
    leaves on a **timeout and nothing else** -- an input transition anywhere in
    the walk would make the lamps depend on whether somebody leant on a lever,
    which is exactly the doubt this graph is meant to remove.
    """
    walk = load_example_graph("state-walk")
    assert walk.entry == "Ready"

    # The trigger is the one place an input is consulted, and it is the entry.
    assert [transition.goto for transition in walk.state_named("Ready").transitions] == ["Step1"]
    assert walk.state_named("Ready").transitions[0].when.all == ["start_switch"]

    walking = [state for state in walk.states if state.name.startswith("Step")]
    assert len(walking) == 6
    for state in walking:
        assert state.transitions == [], f"{state.name} can be left by an input"
        assert state.timeout is not None

    # And the timeouts chain in declaration order, ending in the one terminal
    # state. Asserted by walking it rather than by reading the file, because a
    # `goto` pointing back up the list would still load, still fit, and loop
    # until the trial cap.
    seen = []
    state = walk.state_named("Step1")
    while state.timeout is not None:
        seen.append(state.name)
        state = walk.state_named(state.timeout.goto)
    assert seen == ["Step1", "Step2", "Step3", "Step4", "Step5", "Step6"]
    assert state.name == "Done"
    assert state.outcome == "HIT"


def test_every_step_of_the_walk_is_the_same_500_ms():
    """One distribution, named once and shared by all six.

    Six `fixed` entries saying 500 would spend six of the board's thirty-two
    distribution slots on one number, and would let five of them drift.

    `pause` is the other one and is not a step: it is how long `Done` is held
    before a board arming its own trials walks again.
    """
    walk = load_example_graph("state-walk")
    assert walk.distributions["step"].duration_ms == 500
    for state in walk.states:
        if state.timeout is not None:
            assert state.timeout.after == "step"


def test_the_walk_comes_round_again_on_a_board_running_by_itself():
    """The bench instrument's whole point is being watchable, and a march that
    happens once is one somebody has to keep restarting.

    It costs nothing under triald, which arms every trial itself and never looks
    at the dwell -- which is the property the field was given to the graph for.
    """
    walk = load_example_graph("state-walk")
    assert walk.state_named("Done").relight_after == "pause"
    assert walk.distributions["pause"].duration_ms == 1500


def test_the_walk_leaves_exactly_one_lamp_lit_at_a_time():
    """What makes it readable from across the room.

    Each state raises its own lamp on entry and drops it on exit, rather than
    the next state dropping the previous one's: the second form works until
    somebody reorders the walk, and then two lamps are lit and the graph still
    validates.
    """
    walk = load_example_graph("state-walk")
    for state in walk.states:
        if state.outcome is not None:
            continue
        assert len(state.on_entry) == 1 and state.on_entry[0].kind == "high"
        assert len(state.on_exit) == 1 and state.on_exit[0].kind == "low"
        assert state.on_entry[0].line == state.on_exit[0].line, (
            f"{state.name} raises one line and drops another"
        )


def test_the_walk_touches_no_valve():
    """A demonstration that opened a water valve six times would be a
    demonstration nobody could run twice.

    `reward_valve` is held open by a low and is the reason `safe_level_is_high`
    exists at all; a bench instrument has no business near it.
    """
    walk = load_example_graph("state-walk")
    driven = {action.line for state in walk.states for action in [*state.on_entry, *state.on_exit]}
    assert driven == {"ready_lamp", "cue_lamp", "error_lamp"}


def test_all_three_examples_fit_the_reference_board_together():
    """The walk is meant to be loaded *beside* the paradigms, not instead of
    them -- it is the thing you run when a paradigm is misbehaving."""
    compiled = compile_graph_set_for_device(
        [load_example_graph(name) for name in EXAMPLE_GRAPH_NAMES],
        load_example_line_map(),
        UNO_R4_MINIMA_CAPABILITIES,
        set_version=1,
    )
    for pool_name, used in compiled.pool_usage.items():
        assert used <= compiled.pool_capacity[pool_name], pool_name
    # And with room left, because the next example added should not be the one
    # that discovers the ceiling.
    assert compiled.pool_usage["states"] <= 26
