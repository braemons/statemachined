# SPDX-License-Identifier: LGPL-3.0-or-later
"""What the box is, as opposed to what it is doing today.

`/etc/braemons/statemachined-rig-config.toml`, hand-edited, a package conffile,
and **never written by this daemon**. The name and the split are vstimd's --
`/etc/braemons/vstimd-rig-config.toml` describes itself as "hardware-specific
behaviour that does not change between experiments" -- and the two daemons being
configured the same way on one rig is worth more than either of them being
configured cleverly.

Three rules shape what is in here and what is not.

**A setting is here only if a rig differs by it.** The scan rate is not a
setting -- it is a property of the firmware, measured at boot and reported by the
board. What is here is what one box has and the next one does not: the device it
owns, where its files go, where triald is.

**Nothing a person edits from the web UI is here.** That is the line map and the
graphs, and they live in a state-machine config under
`/var/lib/braemons/statemachined/configs/` (`model/state_machine_config.py`).
The reason is mechanical rather than aesthetic: a conffile the daemon rewrites
is a file that fights dpkg on every upgrade, and the line map is exactly the
thing somebody adjusts at the bench on a Tuesday.

**The paths are the package's, and a bench passes its own.** There is no search
order and no guessing at who started the process: the defaults below are where
the `.deb` puts things, `--config` and the directory settings override them, and
`make bench` passes a conffile naming paths under `build/`. A daemon that
answered "which file am I reading" differently depending on `$HOME` is a daemon
nobody can debug over the phone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import tomllib
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIGURATION_PATH = Path("/etc/braemons/statemachined-rig-config.toml")

#: Everything this daemon writes, under one directory the package owns and
#: systemd's `StateDirectory=` creates. `/var/lib/braemons/<daemon>` is where
#: vstimd keeps its saved configs, and one place to back up beats three.
DEFAULT_STATE_DIRECTORY = Path("/var/lib/braemons/statemachined")


class RigConfiguration(BaseModel):
    """One box's settings."""

    model_config = ConfigDict(extra="forbid")

    #: A device path, a `host:port`, or any pyserial URL. One device per daemon
    #: in v1; a rig with two MCUs is `statemachined@.service`, a systemd
    #: template with a config per instance.
    device_target: str = "/dev/braemons/statemachined0"
    device_baud: int = 115200
    device_timeout_seconds: float = 2.0

    #: What board this rig is supposed to have, as `hello_ack` names it --
    #: `uno_r4_minima`. Checked at connect and refused on a mismatch, because a
    #: different MCU on the same cable is a different pinout wearing the same
    #: pin *names*: a Teensy and an R4 both have an `A0`, and a line map
    #: resolved against the wrong one of them resolves cleanly and drives the
    #: wrong hole. Empty means "do not check", which is what a bench wants.
    expected_board: str = ""

    #: Fixed for the life of the daemon when set, so that a whole session --
    #: including one interrupted by a reconnect -- replays. Empty means one is
    #: drawn per connection, which is right for a rig and wrong for a
    #: reproduction.
    session_seed: str = ""

    #: Whether the daemon opens the port and greets on startup. Off is for a
    #: bench where somebody else is holding the board.
    connect_on_startup: bool = True

    #: Which state-machine config to load on startup, by name. This is what
    #: lets a rig come back from a power cut already wired: the map is applied
    #: and pushed as soon as the board is greeted, so the lines are named and at
    #: their safe levels before anybody opens a browser.
    #:
    #: It does **not** open a session. Putting the graphs on the device is an
    #: act somebody takes -- triald at the top of a session, or a person on a
    #: bench -- and a daemon that armed itself on boot would be a rig that came
    #: back from a power cut ready to run trials nobody had asked for.
    #:
    #: Empty means "come up with nothing loaded", which is right for a bench
    #: and for a box whose experiment changes daily.
    startup_state_machine_config: str = ""

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
    trace_directory: Path = DEFAULT_STATE_DIRECTORY / "trace"

    #: One JSON file per graph, greppable and diffable, because a rig at 2 a.m.
    #: with no network is fixed with an editor. The library a state-machine
    #: config is assembled out of, rather than what a session runs.
    graph_store_directory: Path = DEFAULT_STATE_DIRECTORY / "graphs"

    #: Two files per recording -- the entries and the manifest. Separate from
    #: `trace_directory` because the two have opposite lifetimes: the trace is
    #: rotated by logrotate and is nobody's to keep, and a recording is somebody's
    #: experiment and is deleted only when they say so.
    recording_directory: Path = DEFAULT_STATE_DIRECTORY / "recordings"

    #: One JSON file per saved state-machine config: the line map and the
    #: graphs, written by the web UI. See `state_machine_config_store.py`.
    state_machine_config_directory: Path = DEFAULT_STATE_DIRECTORY / "configs"

    #: Seconds between the heartbeat pings that arm the device's link-loss
    #: watchdog -- and, for free, keep the clock correlation fresh.
    heartbeat_seconds: float = Field(default=2.0, gt=0)

    @classmethod
    def load_from_toml_file(cls, path: Path) -> RigConfiguration:
        """Read one, or the defaults if there is no file.

        A missing file is not an error: a daemon started by hand on a bench has
        no `/etc/braemons`, and refusing to start would make the first thing
        anybody does with this package the thing that fails.
        """
        if not path.exists():
            return cls()
        return cls.model_validate(tomllib.loads(path.read_text()))
