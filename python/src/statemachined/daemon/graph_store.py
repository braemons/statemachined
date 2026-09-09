# SPDX-License-Identifier: LGPL-3.0-or-later
"""Graphs on disk: one JSON file per graph, addressed by name.

docs/developer/daemon.md answers `PLAN.md`'s "where do graphs live in triald?" with
*neither of the two options offered* -- they live here, under
`/var/lib/statemachined/graphs/`, and a triald `TrialType` references one by
name. That keeps N trial types from carrying N copies of a paradigm, and it puts
the graph next to the only process that can validate it against a real device's
`caps`.

One file per graph rather than one database, and it is not laziness. The files
are greppable, diffable and copyable; a rig at two in the morning with no
network is fixed with an editor. A database would buy indexed queries over a
directory that will hold tens of entries.

**Nothing invalid is ever written.** A graph is validated on the way in, so the
store cannot hold a paradigm that could not be run -- which means the failure
surfaces when somebody saves, with the field named, rather than when a session
starts.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..model.graph_definition import GraphDefinition


class GraphNotInStore(KeyError):
    """No graph of that name is stored. The message lists what is."""


class GraphNameMismatch(ValueError):
    """A graph was stored under a name that is not the one inside it."""


class GraphStore:
    """The graphs this rig knows, by name."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path_for_name(self, graph_name: str) -> Path:
        # A name is a file name, so it may not climb out of the directory. This
        # is reachable from an HTTP path parameter, which is exactly the place
        # not to trust one.
        if not graph_name or "/" in graph_name or graph_name.startswith("."):
            raise GraphNameMismatch(f"{graph_name!r} is not a usable graph name")
        return self.directory / f"{graph_name}.json"

    def stored_graph_names(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(path.stem for path in self.directory.glob("*.json"))

    def load(self, graph_name: str) -> GraphDefinition:
        path = self._path_for_name(graph_name)
        if not path.exists():
            known = ", ".join(self.stored_graph_names()) or "(none)"
            raise GraphNotInStore(f"no graph called {graph_name!r} is stored. Stored: {known}")
        graph = GraphDefinition.model_validate(json.loads(path.read_text()))
        if graph.name != graph_name:
            raise GraphNameMismatch(
                f"{path} holds a graph called {graph.name!r}, not {graph_name!r}"
            )
        return graph

    def load_all(self, graph_names: list[str]) -> list[GraphDefinition]:
        """Every named graph, in the order named -- which becomes the slot order."""
        return [self.load(graph_name) for graph_name in graph_names]

    def save(self, graph: GraphDefinition) -> Path:
        """Write one, under its own name.

        Written to a temporary file and renamed, because a half-written graph on
        a rig's disk is a session that fails at startup with a JSON error
        instead of a paradigm.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path_for_name(graph.name)
        temporary_path = path.with_suffix(".json.partial")
        temporary_path.write_text(json.dumps(graph.model_dump(mode="json"), indent=2) + "\n")
        temporary_path.replace(path)
        return path

    def delete(self, graph_name: str) -> None:
        path = self._path_for_name(graph_name)
        if not path.exists():
            known = ", ".join(self.stored_graph_names()) or "(none)"
            raise GraphNotInStore(f"no graph called {graph_name!r} is stored. Stored: {known}")
        path.unlink()
