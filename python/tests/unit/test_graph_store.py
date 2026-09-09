# SPDX-License-Identifier: GPL-3.0-or-later
"""One JSON file per graph, and nothing invalid ever written."""

from __future__ import annotations

import json

import pytest

from statemachined.daemon.graph_store import GraphNameMismatch, GraphNotInStore, GraphStore
from statemachined.model.graph_definition import GraphDefinition


def a_graph(name: str) -> GraphDefinition:
    return GraphDefinition.model_validate(
        {
            "name": name,
            "entry": "Wait",
            "distributions": {"dwell": {"kind": "fixed", "duration_ms": 500}},
            "states": [
                {"name": "Wait", "timeout": {"after": "dwell", "goto": "Hit"}},
                {"name": "Hit", "outcome": "HIT"},
            ],
        }
    )


def test_a_graph_round_trips_through_the_store(tmp_path):
    store = GraphStore(tmp_path)
    store.save(a_graph("go-nogo"))
    assert store.stored_graph_names() == ["go-nogo"]
    assert store.load("go-nogo").entry == "Wait"


def test_the_file_is_json_a_person_can_edit(tmp_path):
    # The whole reason it is a directory of files rather than a database: a rig
    # at 2 a.m. with no network is fixed with an editor.
    store = GraphStore(tmp_path)
    path = store.save(a_graph("go-nogo"))
    assert json.loads(path.read_text())["name"] == "go-nogo"
    assert path.read_text().endswith("\n")


def test_an_empty_store_is_not_an_error(tmp_path):
    assert GraphStore(tmp_path / "not-created-yet").stored_graph_names() == []


def test_a_missing_graph_lists_what_is_stored(tmp_path):
    store = GraphStore(tmp_path)
    store.save(a_graph("go-nogo"))
    with pytest.raises(GraphNotInStore, match="Stored: go-nogo"):
        store.load("2afc")


def test_a_graph_stored_under_the_wrong_name_is_refused(tmp_path):
    # Hand-edited files are expected here, so the mismatch is a thing that will
    # happen -- and a graph loaded under a name it does not answer to would make
    # `configure` name a slot that is not the paradigm anybody meant.
    store = GraphStore(tmp_path)
    (tmp_path / "renamed.json").write_text(json.dumps(a_graph("go-nogo").model_dump(mode="json")))
    with pytest.raises(GraphNameMismatch, match="not 'renamed'"):
        store.load("renamed")


def test_a_name_may_not_climb_out_of_the_directory(tmp_path):
    # Reachable from an HTTP path parameter, which is exactly the place not to
    # trust one.
    store = GraphStore(tmp_path)
    for hostile_name in ("../etc/passwd", "sub/graph", ".hidden", ""):
        with pytest.raises(GraphNameMismatch):
            store.load(hostile_name)


def test_deleting_a_graph_that_is_not_there_says_so(tmp_path):
    with pytest.raises(GraphNotInStore):
        GraphStore(tmp_path).delete("go-nogo")


def test_saving_leaves_no_partial_file_behind(tmp_path):
    store = GraphStore(tmp_path)
    store.save(a_graph("go-nogo"))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["go-nogo.json"]


def test_the_order_names_are_asked_for_is_the_order_they_come_back(tmp_path):
    # It becomes the slot order, so it is not incidental.
    store = GraphStore(tmp_path)
    for name in ("a", "b", "c"):
        store.save(a_graph(name))
    assert [graph.name for graph in store.load_all(["c", "a"])] == ["c", "a"]
