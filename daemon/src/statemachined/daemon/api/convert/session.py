# SPDX-License-Identifier: AGPL-3.0-or-later
"""What this rig is loaded with, and the set on the board.

**Three facts, reported separately rather than collapsed into one `ready`
flag**, because the useful question at two in the morning is *which* of them is
missing: a config may be loaded with no session open, and a set may be
committed on the board from a session that ended — which is normal, and is what
makes a reconnect cheap.
"""

from __future__ import annotations

import time
from typing import Any

from statemachined._proto.statemachined.v1 import session_pb2

from .device import committed_set_to_wire, pool_counts_to_wire


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
    opened_at: float | None,
    active_graph: str | None,
    stored_config_names: list[str],
) -> session_pb2.SessionState:
    message = session_pb2.SessionState(
        session_open=opened_at is not None,
        active_graph=active_graph or "",
        stored_config_names=stored_config_names,
    )
    if opened_at is not None:
        message.opened_at_unix_seconds = opened_at
        # Worked out on this side, against this daemon's own clock. A browser
        # subtracting a rig's timestamp from its own goes negative on a box
        # whose NTP has not settled.
        message.open_seconds = max(0.0, time.time() - opened_at)
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
        pool_usage=pool_counts_to_wire(compiled.pool_usage),
        pool_capacity=pool_counts_to_wire(compiled.pool_capacity),
        elapsed_milliseconds=elapsed_milliseconds,
    )


def loaded_config_result_to_wire(
    *, loaded: str, wiring_pushed: bool, line_map, graph_names: list[str]
) -> session_pb2.LoadedConfigResult:
    """What loading a state-machine config did.

    `wiring_pushed` is a real distinction rather than a courtesy: a config
    loads with nothing attached, the line map is applied, and it reaches the
    board the moment one greets. A caller that assumed the wiring was live
    would be assuming a lamp it cannot see.
    """
    result = session_pb2.LoadedConfigResult(
        loaded=loaded, wiring_pushed=wiring_pushed, graph_names=graph_names
    )
    result.line_map.CopyFrom(line_map)
    return result


def close_session_result_to_wire(
    closed: dict[str, Any], session: session_pb2.SessionState
) -> session_pb2.CloseSessionResult:
    """What closing did, and the session it left behind.

    `cancelled_trial_id` is the one act closing performs on the board, and it
    was answered by the route and dropped when this became an rpc: a caller
    that armed a trial and then closed needs to know that trial is gone.
    """
    result = session_pb2.CloseSessionResult(was_open=bool(closed.get("was_open")))
    result.session.CopyFrom(session)
    cancelled = closed.get("cancelled_trial_id")
    if cancelled is not None:
        result.cancelled_trial_id = cancelled
    return result


def active_graph_to_wire(name: str | None) -> session_pb2.ActiveGraph:
    """Which graph a trial gets when it names none. Empty means nothing is
    selected, in which case a trial must name one."""
    return session_pb2.ActiveGraph(active_graph=name or "")
