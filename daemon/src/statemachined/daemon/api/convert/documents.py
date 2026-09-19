# SPDX-License-Identifier: LGPL-3.0-or-later
"""The stores, as summaries — never as documents.

**A graph, a line map and a state-machine config cross this API as text.**
`proto/statemachined/v1/documents.proto` has the argument; what matters here is
the consequence: there is no `graph_to_wire`, because there is no wire graph.
The model that parses the file is the only description of one, and a second
would be a description that loses the day the two disagree.

What *is* here is the reading a chooser needs — a list with enough of each
entry to pick between them — and the answer a validator gives.

**A document that will not parse is summarised with its reason rather than
left out.** A paradigm that has silently vanished from a list is how somebody
spends an afternoon looking for it, and a file you cannot see is a file you
cannot fix.
"""

from __future__ import annotations

from typing import Any

from statemachined._proto.statemachined.v1 import documents_pb2


def stored_file_to_wire(name: str, text: str) -> documents_pb2.StoredFile:
    return documents_pb2.StoredFile(name=name, text=text)


def graph_summary_to_wire(summary: dict[str, Any]) -> documents_pb2.GraphSummary:
    """One entry of the graph list.

    `readable` rather than the absence of `detail`: a graph could conceivably
    parse and still have something worth saying about it, and a caller should
    branch on the flag rather than on whether a string is empty.
    """
    unreadable = summary.get("detail") if not summary.get("readable", True) else None
    return documents_pb2.GraphSummary(
        name=str(summary.get("name", "")),
        readable=bool(summary.get("readable", True)),
        detail=str(unreadable or ""),
        state_count=int(summary.get("state_count", 0)),
        entry=str(summary.get("entry", "")),
    )


def graph_summaries_to_wire(summaries: list[dict[str, Any]]) -> documents_pb2.GraphSummaries:
    return documents_pb2.GraphSummaries(
        graphs=[graph_summary_to_wire(each) for each in summaries]
    )


def graph_validation_to_wire(result: dict[str, Any]) -> documents_pb2.GraphValidation:
    """What checking a graph found, against this board's capacities.

    `warnings` survive a *valid* answer, deliberately: a graph whose predicate
    can never be true is legal, uploads, runs, and is probably narrower than
    its author thinks. Reported rather than refused.
    """
    return documents_pb2.GraphValidation(
        valid=bool(result.get("valid")),
        detail=str(result.get("detail", "")),
        pool_usage=int(result.get("pool_usage", 0)),
        pool_capacity=int(result.get("pool_capacity", 0)),
        warnings=[str(warning) for warning in result.get("warnings", [])],
    )


def config_summary_to_wire(
    summary: dict[str, Any],
) -> documents_pb2.StateMachineConfigSummary:
    """One entry of the state-machine config list.

    The store says `unreadable` where the graph store says `readable: False`;
    the two grew apart and the message says it one way. Neither store is
    changed for this — that is the seam's job.
    """
    unreadable = summary.get("unreadable")
    return documents_pb2.StateMachineConfigSummary(
        name=str(summary.get("name", "")),
        readable=unreadable is None,
        detail=str(unreadable or ""),
        description=str(summary.get("description", "")),
        board=str(summary.get("board", "")),
        graph_names=[str(name) for name in summary.get("graph_names", [])],
        input_line_count=int(summary.get("input_line_count", 0)),
        output_line_count=int(summary.get("output_line_count", 0)),
    )


def config_summaries_to_wire(
    summaries: list[dict[str, Any]], *, loaded: str | None
) -> documents_pb2.StateMachineConfigSummaries:
    return documents_pb2.StateMachineConfigSummaries(
        configs=[config_summary_to_wire(each) for each in summaries],
        loaded=loaded or "",
    )
