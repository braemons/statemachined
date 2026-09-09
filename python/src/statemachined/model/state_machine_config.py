# SPDX-License-Identifier: LGPL-3.0-or-later
"""Everything the device needs to be a working state machine for an experiment.

The other half of `rig_configuration.py`, and the split is the same one vstimd
makes between its rig-config and its scene-config: **what the box is** against
**what the box is doing today**.

  * The rig config is hand-edited TOML in `/etc/braemons`, it is a package
    conffile, and the daemon never writes it. Device target, triald URL,
    directories, timeouts -- things that are true of the box in the rack
    whatever experiment is running on it.

  * A state-machine config is this: the line map and the graphs, written by the
    web UI, saved under `/var/lib/braemons/statemachined/configs/`. It is what a
    person changes on a Tuesday, so it lives where a daemon may write and a
    package upgrade will not tread.

**Self-contained, and that is a decision rather than an accident.** The graphs
are *in* here, not named and fetched from the store. What that buys is a file
somebody can hand to a colleague, archive beside the session's data, or diff
against the config that was running the week the numbers changed -- which is
the question this file will actually be asked, months later. The graph store
stays what it was: a library you assemble a config out of.

**The price is that a config carries a line map, and a line map is per box.** A
graph is portable because it names `reward_valve`; the map naming which pin that
is describes one rig and no other. So a config moved between rigs is a config
whose map may be wrong -- and the reason that is survivable is
`model/line_map.py`'s pin-only form: the map names `A0`, the daemon resolves it
against the board the moment the config is loaded, and a board that does not
have that pin (or has it as an input) is refused with the board's own pins in
the message. Loud, at load, before a valve moves. A map naming bit positions
would have travelled silently and driven the wrong line.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .graph_definition import GraphDefinition
from .line_map import LineMap

#: What a config may be called on disk. A name is a file name -- see
#: `state_machine_config_store.py` -- so it may not climb out of the directory
#: or hide itself, and it is reachable from an HTTP path parameter, which is
#: exactly the place not to trust one.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class StateMachineConfig(BaseModel):
    """One experiment's wiring and paradigms, as a person saved them."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=NAME_PATTERN)

    #: Free text for the person who opens this in six months. Not used for
    #: anything, which is the point: a field nothing parses is a field nobody
    #: has to keep in a format.
    description: str = ""

    #: The board this was authored against, as `hello_ack` names it --
    #: `uno_r4_minima`. Advisory and checked at load: a config written for one
    #: board and loaded on another is the case the line map cannot always catch
    #: on its own, because two boards can both have a pin called `A0` and mean
    #: different holes. Empty means "did not say", which is not an error.
    board: str = ""

    #: Which pin is the left lever, in the box this was saved on. Written in
    #: pins rather than bit positions -- see the note at the top of this file.
    line_map: LineMap = Field(default_factory=LineMap)

    #: Every graph a session using this config may run, in the order they will
    #: take slots on the device. The whole set goes up before the first trial
    #: (docs/developer/daemon.md §3.2), so this list is what a session *is*.
    graphs: list[GraphDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _refuse_two_graphs_of_one_name(self) -> StateMachineConfig:
        seen: set[str] = set()
        for graph in self.graphs:
            if graph.name in seen:
                # A trial names a graph, never a slot (docs/developer/daemon.md §3.1), so
                # two graphs of one name is a trial whose paradigm depends on
                # which copy the compiler reached first.
                raise ValueError(f"two graphs in this config are called {graph.name!r}")
            seen.add(graph.name)
        return self

    def graph_named(self, graph_name: str) -> GraphDefinition:
        """One graph, or a ValueError naming what this config holds."""
        for graph in self.graphs:
            if graph.name == graph_name:
                return graph
        known = ", ".join(graph.name for graph in self.graphs) or "(none)"
        raise ValueError(
            f"no graph called {graph_name!r} is in the state-machine config "
            f"{self.name!r}. It has: {known}"
        )
