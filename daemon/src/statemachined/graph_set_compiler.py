# SPDX-License-Identifier: LGPL-3.0-or-later
"""Names into indices: the one translation the daemon exists to do.

A person writes `{"when": {"all": ["lever_left"], "none": ["abort"]}, "goto":
"Hit"}`. The device is told `{"all": 16, "none": 4, "target": 3}`, because it
has 32 KB and cannot hold the word "lever_left" (dev/PROTOCOL.md, "Types and
units"). Everything in between happens here.

What comes out is a **plan**, not a conversation: an ordered list of messages and
the tables needed to read a result back. Nothing here touches a link, which is
what lets the whole translation be tested on a host, message by message, against
the protocol document -- and what keeps `statemachined.device` from ever needing
to know what a name is.

Three things are checked here that nowhere else can check them:

  * **The line names exist on this rig.** The graph is portable; the line map is
    not, and a paradigm moved between rigs finds out here rather than by driving
    the wrong valve.
  * **The whole set fits this board.** The pools are shared, so it is the *sum*
    across every graph that matters, and it is checked against the `caps` the
    device declared in `hello_ack` rather than against a constant compiled in
    here. A twelve-graph board and a twenty-graph board are both answered
    correctly by the same code.
  * **Nothing is uploaded that the device would refuse.** The device validates
    again regardless -- it does not trust the host, and a daemon bug must not be
    able to commit a bad set -- but a refusal that names `Foreperiod` before the
    first byte goes out is a better error than one naming state 2 after the
    last.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from .device.message_vocabulary import MsgType
from .model.graph_definition import (
    ChoiceDuration,
    DurationDistribution,
    ExponentialDuration,
    FixedDuration,
    GraphDefinition,
    OutputActionSpecification,
    StateDefinition,
    UniformDuration,
)
from .model.line_map import LineMap
from .model.trial_outcome import terminal_outcome_for_name

#: `kNoTransition` on the wire, and what a `graph_state` with no timeout omits.
NO_TRANSITION_FIRED = 255


class GraphSetCompilationError(Exception):
    """A set that cannot be uploaded, and why in words a paradigm author can act on.

    Every message names the graph, and where it can, the state and the field.
    "Every refusal names what to change" is the rule the firmware's own errors
    follow; this is the same rule one layer up, where the names still exist.
    """


class DeviceCapabilities(BaseModel):
    """What one board can hold, as `hello_ack` declares it.

    Read from the device, never assumed: the reference board ships more than one
    image, and a Teensy is a different set of numbers again. Compiling against a
    constant would mean a graph that fits in the tests and not on the bench.
    """

    model_config = ConfigDict(extra="ignore")

    max_line: int = Field(gt=0)
    max_states: int = Field(gt=0)
    max_transitions: int = Field(ge=0)
    max_output_actions: int = Field(ge=0)
    max_distributions: int = Field(ge=0)
    max_choice_options: int = Field(ge=0)
    max_path: int = Field(gt=0)
    max_graphs: int = Field(gt=0, default=1)

    #: Not part of `caps` on the wire -- they are top-level members of
    #: `hello_ack` -- but they belong to the same question, so
    #: `from_hello_ack` folds them in here.
    input_line_count: int = Field(ge=0, default=32)
    output_line_count: int = Field(ge=0, default=32)

    @classmethod
    def from_hello_ack(cls, hello_ack: dict) -> DeviceCapabilities:
        """The capacities out of a greeting, wherever the wire happens to put them."""
        capabilities = dict(hello_ack.get("caps", {}))
        capabilities["input_line_count"] = hello_ack.get("n_input_lines", 32)
        capabilities["output_line_count"] = hello_ack.get("n_output_lines", 32)
        return cls.model_validate(capabilities)


@dataclass(frozen=True)
class UploadMessage:
    """One line of dev/PROTOCOL.md 3.2, before it is framed.

    A message type and its body, in the order it must be sent. The CRC and the
    rolling checksum belong to the wire and are added there, over the bytes
    actually put on it -- computing them here would mean computing them over
    bytes nobody sent.
    """

    msg_type: MsgType
    fields: dict[str, object]


@dataclass(frozen=True)
class CompiledGraph:
    """One graph, as the device now holds it and as a result can be read back.

    The tables are what make a record legible months later: they are the graph
    that actually ran, kept beside the trial rather than looked up in whatever
    the store holds today.
    """

    name: str
    slot: int
    state_names_by_index: list[str]
    #: For each state, where each of its transitions leads, in the order the
    #: graph declares them -- so a visit's `transition_index` resolves to a
    #: state name without the host knowing the pool's layout.
    transition_target_names_by_state_index: list[list[str]]
    #: Which entry of the set's shared distribution pool each of this graph's
    #: named distributions became. What a per-trial `patch` needs.
    distribution_pool_index_by_name: dict[str, int]

    def state_index_for_name(self, state_name: str) -> int:
        return self.state_names_by_index.index(state_name)


@dataclass(frozen=True)
class CompiledGraphSet:
    """Everything a session needs: what to send, and how to read the answers."""

    set_version: int
    graphs_by_slot: list[CompiledGraph]
    upload_messages: list[UploadMessage]

    #: What this set costs, against what the board had. Reported rather than
    #: only checked, because "you have room for two more graphs" is the useful
    #: form of a capacity, and it is what `POST /api/session/graphs` should say.
    pool_usage: dict[str, int]
    pool_capacity: dict[str, int]

    def graph_named(self, graph_name: str) -> CompiledGraph:
        for graph in self.graphs_by_slot:
            if graph.name == graph_name:
                return graph
        known = ", ".join(graph.name for graph in self.graphs_by_slot)
        raise GraphSetCompilationError(
            f"this set has no graph called {graph_name!r}. It has: {known}"
        )

    def slot_for_graph_name(self, graph_name: str) -> int:
        """What `configure`'s `graph_index` must be for this paradigm.

        The resolution triald never does: it names a graph, and the daemon knows
        which slot the set it built put it in.
        """
        return self.graph_named(graph_name).slot


# --------------------------------------------------------------- private ---


@dataclass
class _SharedDistributionPool:
    """The set-global pool of distributions, filled as graphs are compiled.

    Identical distributions are shared, which is the point of the pool being
    set-global rather than per graph: two paradigms that draw the same 300-700 ms
    foreperiod should cost one of the thirty-two entries, not two. Distributions
    that differ in any parameter are separate entries, so sharing can never
    change what a graph draws.
    """

    entries: list[DurationDistribution] = field(default_factory=list)
    messages: list[UploadMessage] = field(default_factory=list)
    choice_options_used: int = 0

    def index_for(self, distribution: DurationDistribution) -> int:
        for existing_index, existing in enumerate(self.entries):
            if existing == distribution:
                return existing_index
        index = len(self.entries)
        self.entries.append(distribution)
        self.messages.append(
            UploadMessage(MsgType.GRAPH_DIST, _distribution_wire_fields(index, distribution))
        )
        if isinstance(distribution, ChoiceDuration):
            self.choice_options_used += len(distribution.options_ms)
        return index


def _distribution_wire_fields(index: int, distribution: DurationDistribution) -> dict[str, object]:
    """One `graph_dist` body. The wire's `a`/`b`/`c` are positional by `kind`.

    Terse on purpose down there -- see PROTOCOL.md 3.2's table -- and this is the
    only place in the daemon that has to know which parameter is which.
    """
    if isinstance(distribution, FixedDuration):
        return {"i": index, "kind": "fixed", "a": distribution.duration_ms}
    if isinstance(distribution, UniformDuration):
        return {
            "i": index,
            "kind": "uniform",
            "a": distribution.minimum_ms,
            "b": distribution.maximum_ms,
        }
    if isinstance(distribution, ExponentialDuration):
        return {
            "i": index,
            "kind": "exponential",
            "a": distribution.minimum_ms,
            "b": distribution.maximum_ms,
            "c": distribution.mean_ms,
        }
    if isinstance(distribution, ChoiceDuration):
        fields: dict[str, object] = {
            "i": index,
            "kind": "choice",
            "opts": list(distribution.options_ms),
        }
        if distribution.weights is not None:
            fields["weights"] = list(distribution.weights)
        return fields
    raise GraphSetCompilationError(f"no wire form for {type(distribution).__name__}")


def _output_action_wire_fields(
    action: OutputActionSpecification, when: str, line_map: LineMap, graph_name: str
) -> dict[str, object]:
    try:
        line_index = line_map.output_line_index_for_name(action.line)
    except ValueError as exc:
        raise GraphSetCompilationError(f"graph {graph_name!r}: {exc}") from None
    fields: dict[str, object] = {"on": when, "line": line_index, "kind": action.kind}
    if action.kind == "pulse":
        fields["ms"] = action.pulse_ms
    return fields


def _state_wire_fields(
    state: StateDefinition,
    graph: GraphDefinition,
    distribution_pool_index_by_name: dict[str, int],
) -> dict[str, object]:
    """One `graph_state` body.

    `terminal` and `timeout` are always present, as a code or as null: the
    protocol distinguishes absent from null and the device refuses the former,
    rather than guessing which a graph meant.
    """
    terminal_code = None
    if state.outcome is not None:
        terminal_code = int(terminal_outcome_for_name(state.outcome))

    timeout_fields = None
    if state.timeout is not None:
        timeout_fields = {
            "dist": distribution_pool_index_by_name[state.timeout.after],
            # Per graph, counted from zero: the device adds this graph's offset.
            "target": graph.state_names_in_declaration_order.index(state.timeout.goto),
        }

    return {
        "i": graph.state_names_in_declaration_order.index(state.name),
        "terminal": terminal_code,
        "timeout": timeout_fields,
    }


# ---------------------------------------------------------------- public ---


def compile_graph_set_for_device(
    graphs: list[GraphDefinition],
    line_map: LineMap,
    capabilities: DeviceCapabilities,
    set_version: int,
) -> CompiledGraphSet:
    """Turn the graphs a session will use into the upload dev/PROTOCOL.md 3.2 wants.

    The order of `graphs` is the order of the slots, and a slot is what
    `configure`'s `graph_index` names -- so it is stable for the life of the set
    and is the daemon's to assign, never triald's.
    """
    if not graphs:
        raise GraphSetCompilationError("a set needs at least one graph")
    if len(graphs) > capabilities.max_graphs:
        raise GraphSetCompilationError(
            f"this session names {len(graphs)} graphs and the board holds "
            f"{capabilities.max_graphs}"
        )

    duplicate_names = _names_appearing_more_than_once([graph.name for graph in graphs])
    if duplicate_names:
        raise GraphSetCompilationError(
            "a set names the same graph twice: " + ", ".join(sorted(duplicate_names))
        )

    pool = _SharedDistributionPool()
    compiled_graphs: list[CompiledGraph] = []
    graph_upload_messages: list[UploadMessage] = []
    total_state_count = 0
    total_transition_count = 0
    total_output_action_count = 0

    for slot, graph in enumerate(graphs):
        distribution_pool_index_by_name = {
            distribution_name: pool.index_for(distribution)
            for distribution_name, distribution in graph.distributions.items()
        }
        state_names = graph.state_names_in_declaration_order

        graph_upload_messages.append(
            UploadMessage(
                MsgType.GRAPH_BEGIN,
                {
                    "slot": slot,
                    "n_states": len(state_names),
                    "entry": state_names.index(graph.entry),
                },
            )
        )

        transition_targets_by_state_index: list[list[str]] = []
        graph_transition_count = 0
        graph_output_action_count = 0

        for state in graph.states:
            graph_upload_messages.append(
                UploadMessage(
                    MsgType.GRAPH_STATE,
                    _state_wire_fields(state, graph, distribution_pool_index_by_name),
                )
            )

            # Transitions before actions, and every one of a state's own before
            # the next state: the device stores them as a (first, count) slice
            # of a shared pool, so the ordering rule on the wire *is* its memory
            # invariant (PROTOCOL.md 3.2).
            transition_targets_by_state_index.append(
                [transition.goto for transition in state.transitions]
            )
            for transition in state.transitions:
                fields: dict[str, object] = {
                    "target": state_names.index(transition.goto),
                }
                for predicate_key, line_names in (
                    ("all", transition.when.all),
                    ("any", transition.when.any),
                    ("none", transition.when.none),
                ):
                    if not line_names:
                        continue
                    try:
                        fields[predicate_key] = line_map.input_line_mask_for_names(line_names)
                    except ValueError as exc:
                        raise GraphSetCompilationError(
                            f"graph {graph.name!r}, state {state.name!r}: {exc}"
                        ) from None
                if transition.hold is not None:
                    fields["hold"] = distribution_pool_index_by_name[transition.hold]
                if transition.fire_if_already_true_on_entry:
                    fields["level"] = True
                graph_upload_messages.append(UploadMessage(MsgType.GRAPH_TRANSITION, fields))
                graph_transition_count += 1

            # Entry actions before exit actions, always: they are two slices of
            # one pool, and interleaving them would silently give one slice the
            # other's members.
            for when, actions in (("entry", state.on_entry), ("exit", state.on_exit)):
                for action in actions:
                    graph_upload_messages.append(
                        UploadMessage(
                            MsgType.GRAPH_ACTION,
                            _output_action_wire_fields(action, when, line_map, graph.name),
                        )
                    )
                    graph_output_action_count += 1

        graph_upload_messages.append(
            UploadMessage(
                MsgType.GRAPH_END,
                {
                    # This graph's own totals, not the set's: a host that
                    # miscounted one graph should be told which graph.
                    "n_transitions": graph_transition_count,
                    "n_output_actions": graph_output_action_count,
                },
            )
        )

        compiled_graphs.append(
            CompiledGraph(
                name=graph.name,
                slot=slot,
                state_names_by_index=list(state_names),
                transition_target_names_by_state_index=transition_targets_by_state_index,
                distribution_pool_index_by_name=dict(distribution_pool_index_by_name),
            )
        )
        total_state_count += len(state_names)
        total_transition_count += graph_transition_count
        total_output_action_count += graph_output_action_count

    pool_usage = {
        "graphs": len(graphs),
        "states": total_state_count,
        "transitions": total_transition_count,
        "output_actions": total_output_action_count,
        "distributions": len(pool.entries),
        "choice_options": pool.choice_options_used,
    }
    pool_capacity = {
        "graphs": capabilities.max_graphs,
        "states": capabilities.max_states,
        "transitions": capabilities.max_transitions,
        "output_actions": capabilities.max_output_actions,
        "distributions": capabilities.max_distributions,
        "choice_options": capabilities.max_choice_options,
    }
    _refuse_a_set_that_does_not_fit(pool_usage, pool_capacity, graphs)

    upload_messages = [
        UploadMessage(MsgType.SET_BEGIN, {"set_version": set_version, "n_graphs": len(graphs)}),
        # The whole shared pool first, at set level. Legal either way -- a
        # distribution may travel with the graph that introduces it -- but a set
        # that shares entries between graphs has no single graph to put them
        # with, and "the pool belongs to the set" is the truth worth showing.
        *pool.messages,
        *graph_upload_messages,
        UploadMessage(
            MsgType.SET_END,
            {
                "n_states": total_state_count,
                "n_transitions": total_transition_count,
                "n_output_actions": total_output_action_count,
                # `checksum` is added by the uploader, over the bytes it sends.
            },
        ),
    ]

    return CompiledGraphSet(
        set_version=set_version,
        graphs_by_slot=compiled_graphs,
        upload_messages=upload_messages,
        pool_usage=pool_usage,
        pool_capacity=pool_capacity,
    )


def _names_appearing_more_than_once(names: list[str]) -> set[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for name in names:
        if name in seen:
            repeated.add(name)
        seen.add(name)
    return repeated


def _refuse_a_set_that_does_not_fit(
    pool_usage: dict[str, int], pool_capacity: dict[str, int], graphs: list[GraphDefinition]
) -> None:
    """Name what overflowed, and by how much, and which graphs are in the set.

    The failure this prevents is discovering at trial 40 that one trial type
    names a graph with forty states on a thirty-two-state board, and losing the
    session to it -- so the message has to be actionable at the moment a session
    is being set up, minutes before an animal is in the booth.
    """
    overflowing = [
        (pool_name, used, pool_capacity[pool_name])
        for pool_name, used in pool_usage.items()
        if used > pool_capacity[pool_name]
    ]
    if not overflowing:
        return
    listed = "; ".join(
        f"{pool_name}: {used} needed, {capacity} available"
        for pool_name, used, capacity in overflowing
    )
    graph_names = ", ".join(graph.name for graph in graphs)
    raise GraphSetCompilationError(
        f"this session's graphs do not fit the board. {listed}. The set is: {graph_names}"
    )
