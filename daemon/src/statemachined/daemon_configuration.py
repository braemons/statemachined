# SPDX-License-Identifier: LGPL-3.0-or-later
"""Everything a rig differs by, in one file a person can read and edit.

`/etc/braemons/statemachined.toml`, which is a conffile: a package upgrade must
not silently replace the line map somebody spent an afternoon getting right.

Two rules shape what is in here and what is not.

**A setting is here only if a rig differs by it.** The scan rate is not a
setting -- it is a property of the firmware, measured at boot and reported by
the board. The graph store's path is not a setting either; it is where the
package puts it. What is here is what one box has and the next one does not: the
device it owns, the rig it is wired into, where triald is.

**The line map is configuration, not data.** It says which pin is the left lever
in *this* box. Graphs are portable and live in the store; the map that makes one
of them runnable here does not travel, so it lives beside the target URL rather
than beside the paradigms.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .model.line_map import LineMap

DEFAULT_CONFIGURATION_PATH = Path("/etc/braemons/statemachined.toml")


class DaemonConfiguration(BaseModel):
    """One rig's settings."""

    model_config = ConfigDict(extra="forbid")

    #: A device path, a `host:port`, or any pyserial URL. One device per daemon
    #: in v1; a rig with two MCUs is `statemachined@.service`, a systemd
    #: template with a config per instance.
    device_target: str = "/dev/braemons/statemachined0"
    device_baud: int = 115200
    device_timeout_seconds: float = 2.0

    #: Where to report an outcome. Empty means "report nowhere", which is what a
    #: bench box wants and what makes the daemon usable without triald running.
    triald_base_url: str = ""

    #: Fixed for the life of the daemon when set, so that a whole session --
    #: including one interrupted by a reconnect -- replays. Empty means one is
    #: drawn per connection, which is right for a rig and wrong for a
    #: reproduction.
    session_seed: str = ""

    #: Whether the daemon opens the port and greets on startup. Off is for a
    #: bench where somebody else is holding the board.
    connect_on_startup: bool = True

    #: dev/DAEMON.md §3.2. "set" uploads every graph a session uses once and
    #: switches by index; "per_trial" uploads on configure and pays the ITI cost
    #: that mode exists to avoid. An explicit choice, never a silent fallback.
    graph_mode: Literal["set", "per_trial"] = "set"

    #: How many trace entries the ring holds. dev/DAEMON.md §4.6: a normal
    #: session is a few thousand, and the pathological case is a looping
    #: paradigm at the device's 255-visit ceiling for a thousand trials.
    trace_ring_entries: int = Field(default=100_000, gt=0)

    #: Where the NDJSON tail of the trace goes. The daemon never reads it back:
    #: it is the copy for the analysis that happens months later.
    trace_directory: Path = Path("/var/lib/statemachined/trace")

    #: One JSON file per graph, greppable and diffable, because a rig at 2 a.m.
    #: with no network is fixed with an editor.
    graph_store_directory: Path = Path("/var/lib/statemachined/graphs")

    #: What this rig is wired like. See the note at the top of this file.
    line_map: LineMap = Field(default_factory=LineMap)

    #: Seconds between the heartbeat pings that arm the device's link-loss
    #: watchdog -- and, for free, keep the clock correlation fresh.
    heartbeat_seconds: float = Field(default=2.0, gt=0)

    @classmethod
    def load_from_toml_file(cls, path: Path) -> DaemonConfiguration:
        """Read one, or the defaults if there is no file.

        A missing file is not an error: a daemon started by hand on a bench has
        no `/etc/braemons`, and refusing to start would make the first thing
        anybody does with this package the thing that fails.
        """
        if not path.exists():
            return cls()
        return cls.model_validate(tomllib.loads(path.read_text()))
