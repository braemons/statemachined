# SPDX-License-Identifier: LGPL-3.0-or-later
"""What this rig is loaded with, and the set on the board.

**Three facts, reported separately rather than collapsed into one `ready`
flag**, because the useful question at two in the morning is *which* of them is
missing: a config may be loaded with no session open, and a set may be
committed on the board from a session that ended — which is normal, and is what
makes a reconnect cheap.
"""

from __future__ import annotations

from typing import Any

from statemachined._proto.statemachined.v1 import session_pb2

from .device import committed_set_to_wire


def loaded_config_to_wire(
    config: Any | None, *, stored_names: list[str]
) -> session_pb2.LoadedStateMachineConfig | None:
    """The config this rig is running, or nothing.

    `still_in_the_store` is a real question: a loaded config can be deleted
    while it is loaded, and the rig keeps running it. Saying so is better than
    implying the store is the only place it exists.
    """
    if config is None:
        return None
    return session_pb2.LoadedStateMachineConfig(
        name=config.name,
        description=config.description,
        board=config.board,
        graph_names=[graph.name for graph in config.graphs],
        still_in_the_store=config.name in stored_names,
    )


def session_state_to_wire(
    *,
    config: Any | None,
    committed: Any | None,
    session_open: bool,
    active_graph: str | None,
    stored_config_names: list[str],
) -> session_pb2.SessionState:
    message = session_pb2.SessionState(
        session_open=session_open,
        active_graph=active_graph or "",
        stored_config_names=stored_config_names,
    )
    loaded = loaded_config_to_wire(config, stored_names=stored_config_names)
    if loaded is not None:
        message.state_machine_config.CopyFrom(loaded)
    committed_set = committed_set_to_wire(committed)
    if committed_set is not None:
        message.committed_set.CopyFrom(committed_set)
    return message


def open_session_result_to_wire(
    compiled: Any, *, state_machine_config: str, elapsed_milliseconds: int
) -> session_pb2.OpenSessionResult:
    """What putting the graphs on the device cost.

    `slots` is here although no caller needs it — everything else in this API
    takes a name — because it is what a person compares against the board when
    a trial reports the wrong graph.
    """
    return session_pb2.OpenSessionResult(
        state_machine_config=state_machine_config,
        set_version=compiled.set_version,
        slots={graph.name: graph.slot for graph in compiled.graphs_by_slot},
        pool_usage=compiled.pool_usage,
        pool_capacity=compiled.pool_capacity,
        elapsed_milliseconds=elapsed_milliseconds,
    )


def active_graph_to_wire(name: str | None) -> session_pb2.ActiveGraph:
    """Which graph a trial gets when it names none. Empty means nothing is
    selected, in which case a trial must name one."""
    return session_pb2.ActiveGraph(active_graph=name or "")
