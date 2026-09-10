# SPDX-License-Identifier: GPL-3.0-or-later
"""The translation, message by message, against docs/reference/protocol.md 3.2.

These assert the *bytes' shape* rather than only that compilation succeeded,
because the compiler's whole job is to produce an upload the device will accept,
and the device is not here. What is checked against real silicon is the same
code, by the hardware suite; what is checked here is that it says what the
protocol document says it should.
"""

from __future__ import annotations

import pytest

from statemachined.graph_set_compiler import (
    DeviceCapabilities,
    GraphSetCompilationError,
    compile_graph_set_for_device,
)
from statemachined.device.message_vocabulary import MsgType
from statemachined.model.graph_definition import GraphDefinition, UniformDuration
from statemachined.model.line_map import LineMap


def reference_board_capabilities(**overrides) -> DeviceCapabilities:
    """The Uno R4 Minima's, as `hello_ack` declares them."""
    capabilities = {
        "max_line": 512,
        "max_states": 32,
        "max_transitions": 64,
        "max_output_actions": 64,
        "max_distributions": 32,
        "max_choice_options": 32,
        "max_path": 255,
        "max_graphs": 20,
        "max_timers": 8,
        "first_timer_line": 24,
        "input_line_count": 8,
        "output_line_count": 8,
    }
    capabilities.update(overrides)
    return DeviceCapabilities.model_validate(capabilities)


def rig_line_map() -> LineMap:
    return LineMap.model_validate(
        {
            "input_lines": [
                {"name": "start_switch", "line_index": 0},
                {"name": "abort", "line_index": 1},
                {"name": "lever_left", "line_index": 4},
                {"name": "lever_right", "line_index": 5},
            ],
            "output_lines": [
                {"name": "ready_lamp", "line_index": 0},
                {"name": "reward_valve", "line_index": 3},
            ],
        }
    )


def graph_named(name: str, foreperiod_ms: int = 500, **overrides) -> GraphDefinition:
    """Wait --(foreperiod)--> Cue --(a lever)--> Hit, with a lamp and a reward."""
    definition = {
        "name": name,
        "entry": "Wait",
        "distributions": {"foreperiod": {"kind": "fixed", "duration_ms": foreperiod_ms}},
        "states": [
            {
                "name": "Wait",
                "on_entry": [{"line": "ready_lamp", "kind": "high"}],
                "timeout": {"after": "foreperiod", "goto": "Cue"},
                "transitions": [{"when": {"all": ["abort"]}, "goto": "Aborted"}],
            },
            {
                "name": "Cue",
                "transitions": [
                    {
                        "when": {"all": ["lever_left"], "none": ["abort"]},
                        "goto": "Hit",
                    }
                ],
                "timeout": {"after": "foreperiod", "goto": "Aborted"},
            },
            {
                "name": "Hit",
                "outcome": "HIT",
                "on_entry": [{"line": "reward_valve", "kind": "pulse", "pulse_ms": 40}],
            },
            {"name": "Aborted", "outcome": "CANCELLED"},
        ],
    }
    definition.update(overrides)
    return GraphDefinition.model_validate(definition)


def compile_one(graph: GraphDefinition, **overrides):
    return compile_graph_set_for_device(
        [graph], rig_line_map(), reference_board_capabilities(**overrides), set_version=7
    )


def message_types(compiled) -> list[str]:
    return [str(message.msg_type) for message in compiled.upload_messages]


# ------------------------------------------------------- the message order ---


def test_the_upload_is_the_order_the_protocol_states():
    # set_begin, the shared pool, then each graph as its own begin..end, then
    # set_end. The ordering rule on the wire *is* the device's memory
    # invariant: a state's transitions and actions are a (first, count) slice.
    compiled = compile_one(graph_named("go-nogo"))
    assert message_types(compiled) == [
        "set_begin",
        "graph_dist",
        "graph_begin",
        "graph_state",  # Wait
        "graph_transition",
        "graph_action",
        "graph_state",  # Cue
        "graph_transition",
        "graph_state",  # Hit
        "graph_action",
        "graph_state",  # Aborted
        "graph_end",
        "set_end",
    ]


def test_set_begin_declares_the_version_and_how_many_graphs_follow():
    compiled = compile_one(graph_named("go-nogo"))
    assert compiled.upload_messages[0].fields == {"set_version": 7, "n_graphs": 1}


def test_graph_begin_carries_its_slot_and_its_entry_state_by_index():
    compiled = compile_one(graph_named("go-nogo"))
    graph_begin = next(m for m in compiled.upload_messages if m.msg_type == MsgType.GRAPH_BEGIN)
    assert graph_begin.fields == {"slot": 0, "n_states": 4, "entry": 0}


def test_a_state_carries_terminal_and_timeout_as_null_rather_than_omitting_them():
    # The protocol distinguishes absent from null and the device refuses the
    # former, rather than guessing which a graph meant.
    compiled = compile_one(graph_named("go-nogo"))
    states = [m for m in compiled.upload_messages if m.msg_type == MsgType.GRAPH_STATE]
    assert states[0].fields == {"i": 0, "terminal": None, "timeout": {"dist": 0, "target": 1}}
    assert states[3].fields == {"i": 3, "terminal": 10, "timeout": None}  # CANCELLED


def test_a_dwell_after_a_terminal_state_becomes_a_distribution_index():
    # The inter-trial interval, as an index into the shared pool like every
    # other duration. Only terminal states carry one, and it is the whole of
    # what a graph says about running unattended -- who acts on it is a device
    # setting (docs/reference/protocol.md 3.7), not a field here.
    graph = graph_named("go-nogo")
    graph.distributions["iti"] = UniformDuration(minimum_ms=1000, maximum_ms=2000)
    graph.state_named("Hit").relight_after = "iti"
    compiled = compile_one(GraphDefinition.model_validate(graph.model_dump()))
    states = [m for m in compiled.upload_messages if m.msg_type == MsgType.GRAPH_STATE]
    assert states[2].fields["relight"] == 1  # foreperiod is 0, iti is 1
    # And a state with no dwell says nothing at all rather than sending null:
    # absent is unambiguous here, and it is what every graph written before this
    # field existed sends.
    assert "relight" not in states[3].fields
    assert "relight" not in states[0].fields


def test_a_predicate_becomes_masks_and_a_target_becomes_an_index():
    compiled = compile_one(graph_named("go-nogo"))
    transitions = [m for m in compiled.upload_messages if m.msg_type == MsgType.GRAPH_TRANSITION]
    # Cue's: all=lever_left (line 4), none=abort (line 1), to Hit (state 2).
    assert transitions[1].fields == {"target": 2, "all": 1 << 4, "none": 1 << 1}


def test_an_empty_predicate_term_is_left_out_rather_than_sent_as_zero():
    # `any: 0` and `any` absent mean the same thing to the device, and the
    # shorter line is the one that fits inside the 512-byte budget.
    compiled = compile_one(graph_named("go-nogo"))
    transitions = [m for m in compiled.upload_messages if m.msg_type == MsgType.GRAPH_TRANSITION]
    assert "any" not in transitions[0].fields


def test_a_pulse_carries_its_width_and_a_level_action_does_not():
    compiled = compile_one(graph_named("go-nogo"))
    actions = [m for m in compiled.upload_messages if m.msg_type == MsgType.GRAPH_ACTION]
    assert actions[0].fields == {"on": "entry", "line": 0, "kind": "high"}
    assert actions[1].fields == {"on": "entry", "line": 3, "kind": "pulse", "ms": 40}


def test_graph_end_carries_that_graph_s_own_totals():
    # Per graph, not per set: a host that miscounted one graph should be told
    # which graph.
    compiled = compile_one(graph_named("go-nogo"))
    graph_end = next(m for m in compiled.upload_messages if m.msg_type == MsgType.GRAPH_END)
    assert graph_end.fields == {"n_transitions": 2, "n_output_actions": 2}


def test_set_end_carries_the_set_s_totals_and_no_checksum():
    # The checksum is over the bytes actually sent, so it can only be computed
    # by whoever sends them.
    compiled = compile_one(graph_named("go-nogo"))
    set_end = compiled.upload_messages[-1]
    assert set_end.msg_type == MsgType.SET_END
    assert set_end.fields == {"n_states": 4, "n_transitions": 2, "n_output_actions": 2}
    assert "checksum" not in set_end.fields


def test_a_hold_and_a_level_transition_carry_their_wire_fields():
    graph = graph_named("held")
    graph = GraphDefinition.model_validate(
        graph.model_dump()
        | {
            "states": [
                {
                    "name": "Wait",
                    "timeout": {"after": "foreperiod", "goto": "Hit"},
                    "transitions": [
                        {
                            "when": {"all": ["lever_left"]},
                            "goto": "Hit",
                            "hold": "foreperiod",
                            "fire_if_already_true_on_entry": True,
                        }
                    ],
                },
                {"name": "Hit", "outcome": "HIT"},
            ]
        }
    )
    compiled = compile_one(graph)
    transition = next(m for m in compiled.upload_messages if m.msg_type == MsgType.GRAPH_TRANSITION)
    assert transition.fields == {"target": 1, "all": 1 << 4, "hold": 0, "level": True}


# ------------------------------------------------------------------ sets ---


def test_a_set_gives_each_graph_a_slot_in_the_order_it_was_named():
    # The slot is what `configure`'s graph_index names, and it is the daemon's
    # to assign: triald says "go-nogo" and never learns it was slot 0.
    compiled = compile_graph_set_for_device(
        [graph_named("go-nogo"), graph_named("2afc")],
        rig_line_map(),
        reference_board_capabilities(),
        set_version=3,
    )
    assert [(g.name, g.slot) for g in compiled.graphs_by_slot] == [("go-nogo", 0), ("2afc", 1)]
    assert compiled.slot_for_graph_name("2afc") == 1


def test_two_graphs_that_draw_the_same_duration_share_one_pool_entry():
    # The point of the pool being set-global rather than per graph: thirty-two
    # entries is the scarcest thing on the board, and two paradigms that want
    # the same 500 ms foreperiod should cost one of them.
    compiled = compile_graph_set_for_device(
        [graph_named("go-nogo"), graph_named("2afc")],
        rig_line_map(),
        reference_board_capabilities(),
        set_version=3,
    )
    assert compiled.pool_usage["distributions"] == 1
    assert compiled.graphs_by_slot[0].distribution_pool_index_by_name == {"foreperiod": 0}
    assert compiled.graphs_by_slot[1].distribution_pool_index_by_name == {"foreperiod": 0}


def test_durations_that_differ_in_any_parameter_are_separate_entries():
    # Sharing must never be able to change what a graph draws.
    compiled = compile_graph_set_for_device(
        [graph_named("go-nogo", foreperiod_ms=500), graph_named("2afc", foreperiod_ms=900)],
        rig_line_map(),
        reference_board_capabilities(),
        set_version=3,
    )
    assert compiled.pool_usage["distributions"] == 2
    assert compiled.graphs_by_slot[1].distribution_pool_index_by_name == {"foreperiod": 1}


def test_each_graph_numbers_its_own_states_from_zero():
    # The device adds the graph's offset. A host that authored a four-state
    # paradigm must never see where its slice of the shared pool sits.
    compiled = compile_graph_set_for_device(
        [graph_named("go-nogo"), graph_named("2afc")],
        rig_line_map(),
        reference_board_capabilities(),
        set_version=3,
    )
    state_indices = [
        m.fields["i"] for m in compiled.upload_messages if m.msg_type == MsgType.GRAPH_STATE
    ]
    assert state_indices == [0, 1, 2, 3, 0, 1, 2, 3]


def test_naming_the_same_graph_twice_in_a_set_is_refused():
    with pytest.raises(GraphSetCompilationError, match="names the same graph twice"):
        compile_graph_set_for_device(
            [graph_named("go-nogo"), graph_named("go-nogo")],
            rig_line_map(),
            reference_board_capabilities(),
            set_version=3,
        )


# ------------------------------------------------------------ capacities ---


def test_a_set_that_does_not_fit_names_the_pool_and_the_numbers():
    # The failure this prevents is discovering at trial 40 that one trial type
    # names a graph with forty states on a thirty-two-state board.
    with pytest.raises(GraphSetCompilationError, match=r"states: 4 needed, 3 available"):
        compile_one(graph_named("go-nogo"), max_states=3)


def test_the_pools_are_summed_across_the_set_not_checked_per_graph():
    # Two graphs of four states each need eight, because they share the pool.
    # Checking them separately would accept a session that cannot be uploaded.
    with pytest.raises(GraphSetCompilationError, match=r"states: 8 needed, 6 available"):
        compile_graph_set_for_device(
            [graph_named("go-nogo"), graph_named("2afc")],
            rig_line_map(),
            reference_board_capabilities(max_states=6),
            set_version=3,
        )


def test_more_graphs_than_slots_is_refused_before_anything_is_counted():
    with pytest.raises(GraphSetCompilationError, match="names 2 graphs and the board holds 1"):
        compile_graph_set_for_device(
            [graph_named("go-nogo"), graph_named("2afc")],
            rig_line_map(),
            reference_board_capabilities(max_graphs=1),
            set_version=3,
        )


def test_what_a_set_costs_is_reported_and_not_only_checked():
    # "You have room for two more graphs" is the useful form of a capacity, and
    # it is what POST /api/session/graphs should be able to say.
    compiled = compile_one(graph_named("go-nogo"))
    assert compiled.pool_usage == {
        "graphs": 1,
        "states": 4,
        "transitions": 2,
        "output_actions": 2,
        "distributions": 1,
        "choice_options": 0,
    }
    assert compiled.pool_capacity["states"] == 32


def test_capabilities_are_read_from_a_greeting_rather_than_assumed():
    # The reference board ships more than one image and a Teensy is a different
    # set of numbers again. Compiling against a constant would mean a graph that
    # fits in the tests and not on the bench.
    capabilities = DeviceCapabilities.from_hello_ack(
        {
            "n_input_lines": 8,
            "n_output_lines": 8,
            "caps": {
                "max_line": 512,
                "max_states": 32,
                "max_transitions": 64,
                "max_output_actions": 64,
                "max_distributions": 32,
                "max_choice_options": 32,
                "max_path": 255,
                "max_graphs": 20,
            },
        }
    )
    assert capabilities.max_graphs == 20
    assert capabilities.output_line_count == 8


# ------------------------------------------------------------- the names ---


def test_a_line_name_this_rig_does_not_have_names_the_graph_and_the_state():
    # A paradigm is portable and a line map is not, so this is what a graph
    # moved between rigs hits -- and it must not be found by driving the wrong
    # valve.
    graph = GraphDefinition.model_validate(
        {
            "name": "go-nogo",
            "entry": "Wait",
            "distributions": {"foreperiod": {"kind": "fixed", "duration_ms": 500}},
            "states": [
                {
                    "name": "Wait",
                    "timeout": {"after": "foreperiod", "goto": "Hit"},
                    "transitions": [{"when": {"all": ["paw"]}, "goto": "Hit"}],
                },
                {"name": "Hit", "outcome": "HIT"},
            ],
        }
    )
    with pytest.raises(GraphSetCompilationError, match="graph 'go-nogo', state 'Wait'.*'paw'"):
        compile_one(graph)


def test_an_output_line_this_rig_does_not_have_names_the_graph():
    graph = GraphDefinition.model_validate(
        {
            "name": "go-nogo",
            "entry": "Wait",
            "distributions": {"foreperiod": {"kind": "fixed", "duration_ms": 500}},
            "states": [
                {
                    "name": "Wait",
                    "timeout": {"after": "foreperiod", "goto": "Hit"},
                    "on_entry": [{"line": "houselight", "kind": "high"}],
                },
                {"name": "Hit", "outcome": "HIT"},
            ],
        }
    )
    with pytest.raises(GraphSetCompilationError, match="graph 'go-nogo'.*'houselight'"):
        compile_one(graph)


def test_the_compiled_graph_can_read_a_result_back():
    # The tables are what make a record legible months later: they are the graph
    # that actually ran, kept beside the trial rather than looked up in whatever
    # the store holds today.
    compiled = compile_one(graph_named("go-nogo")).graphs_by_slot[0]
    assert compiled.state_names_by_index == ["Wait", "Cue", "Hit", "Aborted"]
    assert compiled.transition_target_names_by_state_index == [["Aborted"], ["Hit"], [], []]


# --------------------------------------------------------- global timers ---


def a_graph_with_a_timer(**timer_overrides) -> GraphDefinition:
    """One state, one timer, and enough distributions to name."""
    timer = {"width": "open", "delay": "foreperiod", "line": "reward_valve"}
    timer.update(timer_overrides)
    return GraphDefinition.model_validate(
        {
            "name": "with-a-timer",
            "entry": "done",
            "distributions": {
                "foreperiod": {"kind": "fixed", "duration_ms": 200},
                "open": {"kind": "fixed", "duration_ms": 40},
            },
            "timers": {"reward": timer},
            "states": [
                {
                    "name": "done",
                    "outcome": "HIT",
                    "on_entry": [{"kind": "timer_start", "timer": "reward"}],
                }
            ],
        }
    )


def messages_of(compiled, msg_type) -> list[dict]:
    return [m.fields for m in compiled.upload_messages if m.msg_type == msg_type]


def test_a_timer_becomes_a_graph_timer_message_naming_pooled_distributions():
    compiled = compile_graph_set_for_device(
        [a_graph_with_a_timer()], rig_line_map(), reference_board_capabilities(), set_version=1
    )
    timers = messages_of(compiled, MsgType.GRAPH_TIMER)
    assert len(timers) == 1
    # Distributions by pool index, the output line by rig index, and nothing
    # sent for the fields that were left at their defaults.
    assert timers[0] == {"i": 0, "width": 1, "delay": 0, "line": 3}


def test_a_timer_start_action_names_a_timer_and_not_a_line():
    # Two index spaces of different sizes. The device bounds-checks them
    # separately, so the wire has to say which one it means.
    compiled = compile_graph_set_for_device(
        [a_graph_with_a_timer()], rig_line_map(), reference_board_capabilities(), set_version=1
    )
    actions = messages_of(compiled, MsgType.GRAPH_ACTION)
    assert actions == [{"on": "entry", "kind": "timer_start", "timer": 0}]


def test_the_timer_message_precedes_the_states_that_name_it():
    compiled = compile_graph_set_for_device(
        [a_graph_with_a_timer()], rig_line_map(), reference_board_capabilities(), set_version=1
    )
    order = [m.msg_type for m in compiled.upload_messages]
    assert order.index(MsgType.GRAPH_TIMER) < order.index(MsgType.GRAPH_STATE)
    # And after the distributions it names, which the device checks.
    assert order.index(MsgType.GRAPH_DIST) < order.index(MsgType.GRAPH_TIMER)


def test_a_predicate_may_name_a_timer_because_a_running_timer_is_a_high_line():
    # The whole design in one assertion: "when the reward timer ends" is
    # `none: [reward]`, and it compiles to a bit of the same mask a lever does.
    graph = GraphDefinition.model_validate(
        {
            "name": "waits-on-a-timer",
            "entry": "wait",
            "distributions": {"open": {"kind": "fixed", "duration_ms": 40}},
            "timers": {"reward": {"width": "open"}},
            "states": [
                {
                    "name": "wait",
                    "transitions": [{"when": {"none": ["reward"], "all": ["lever_left"]}, "goto": "done"}],
                },
                {"name": "done", "outcome": "HIT"},
            ],
        }
    )
    compiled = compile_graph_set_for_device(
        [graph], rig_line_map(), reference_board_capabilities(), set_version=1
    )
    transitions = messages_of(compiled, MsgType.GRAPH_TRANSITION)
    assert transitions[0]["none"] == 1 << 24  # first_timer_line + 0
    assert transitions[0]["all"] == 1 << 4  # lever_left, in the same word


def test_a_timer_the_board_cannot_hold_is_refused_with_both_numbers():
    graph = a_graph_with_a_timer()
    with pytest.raises(GraphSetCompilationError, match="1 global timers and the board holds 0"):
        compile_graph_set_for_device(
            [graph],
            rig_line_map(),
            reference_board_capabilities(max_timers=0),
            set_version=1,
        )


def test_a_timer_sharing_a_name_with_an_input_line_is_refused():
    # One namespace, because the device has one word. Resolving the collision by
    # a precedence rule would make every predicate naming it ambiguous to read.
    graph = GraphDefinition.model_validate(
        {
            "name": "shadowed",
            "entry": "done",
            "distributions": {"open": {"kind": "fixed", "duration_ms": 40}},
            "timers": {"abort": {"width": "open"}},
            "states": [{"name": "done", "outcome": "HIT"}],
        }
    )
    with pytest.raises(GraphSetCompilationError, match="both an input line and a global timer"):
        compile_graph_set_for_device(
            [graph], rig_line_map(), reference_board_capabilities(), set_version=1
        )


def test_an_action_naming_a_timer_that_does_not_exist_is_refused():
    graph = GraphDefinition.model_validate(
        {
            "name": "bad-action",
            "entry": "done",
            "distributions": {"open": {"kind": "fixed", "duration_ms": 40}},
            "states": [
                {
                    "name": "done",
                    "outcome": "HIT",
                    "on_entry": [{"kind": "timer_start", "timer": "nobody"}],
                }
            ],
        }
    )
    with pytest.raises(GraphSetCompilationError, match="no global timer called 'nobody'"):
        compile_graph_set_for_device(
            [graph], rig_line_map(), reference_board_capabilities(), set_version=1
        )
