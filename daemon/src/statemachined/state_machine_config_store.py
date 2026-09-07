# SPDX-License-Identifier: LGPL-3.0-or-later
"""State-machine configs on disk: one JSON file per config, addressed by name.

`/var/lib/braemons/statemachined/configs/`, beside the graph store and the
trace, and deliberately not in `/etc`: this is the file the web UI writes, and a
package conffile the daemon rewrites is a file that fights dpkg on every
upgrade. `/etc/braemons/statemachined-rig-config.toml` stays hand-edited and
untouched by this process; everything a person changes on a Tuesday is here.

The same shape as `graph_store.py`, for the same reasons -- one file per config
rather than a database, greppable and diffable and fixable with an editor at two
in the morning; validated on the way in, so the store cannot hold a config that
could not be loaded; written to a temporary file and renamed, so a config
half-written when the power went is not a daemon that will not start.

The suffix is `.config.json`, which is what vstimd's scene-configs use. Same
family, same convention, and a directory listing says which files are configs
without opening one.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model.state_machine_config import StateMachineConfig

#: What a saved config is called on disk. Two extensions rather than one so the
#: name is still a name -- `go-nogo.config.json` is the config `go-nogo`, and
#: `Path.stem` would answer `go-nogo.config`, which is why the length is taken
#: off explicitly below rather than by `.stem`.
CONFIG_SUFFIX = ".config.json"


class ConfigNotInStore(KeyError):
    """No config of that name is stored. The message lists what is."""


class ConfigNameMismatch(ValueError):
    """A config was stored under a name that is not the one inside it."""


class StateMachineConfigStore:
    """The state-machine configs this rig has saved, by name."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path_for_name(self, config_name: str) -> Path:
        # A name is a file name, so it may not climb out of the directory. This
        # is reachable from an HTTP path parameter, which is exactly the place
        # not to trust one. `StateMachineConfig.name` carries the same rule as a
        # pattern; this is the half that guards a *read*, where no model has
        # been built yet.
        if not config_name or "/" in config_name or config_name.startswith("."):
            raise ConfigNameMismatch(f"{config_name!r} is not a usable config name")
        return self.directory / f"{config_name}{CONFIG_SUFFIX}"

    def stored_config_names(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(
            path.name[: -len(CONFIG_SUFFIX)] for path in self.directory.glob(f"*{CONFIG_SUFFIX}")
        )

    def load(self, config_name: str) -> StateMachineConfig:
        path = self._path_for_name(config_name)
        if not path.exists():
            known = ", ".join(self.stored_config_names()) or "(none)"
            raise ConfigNotInStore(
                f"no state-machine config called {config_name!r} is stored. Stored: {known}"
            )
        config = StateMachineConfig.model_validate(json.loads(path.read_text()))
        if config.name != config_name:
            raise ConfigNameMismatch(
                f"{path} holds a config called {config.name!r}, not {config_name!r}"
            )
        return config

    def save(self, config: StateMachineConfig) -> Path:
        """Write one, under its own name.

        Written to a temporary file and renamed, because a half-written config
        on a rig's disk is a rig that comes up with no wiring at all -- and the
        line map is the one thing a board cannot be asked for.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path_for_name(config.name)
        temporary_path = path.with_name(path.name + ".partial")
        temporary_path.write_text(json.dumps(config.model_dump(mode="json"), indent=2) + "\n")
        temporary_path.replace(path)
        return path

    def delete(self, config_name: str) -> None:
        path = self._path_for_name(config_name)
        if not path.exists():
            known = ", ".join(self.stored_config_names()) or "(none)"
            raise ConfigNotInStore(
                f"no state-machine config called {config_name!r} is stored. Stored: {known}"
            )
        path.unlink()

    def summaries(self) -> list[dict]:
        """Every config, as much of each as a chooser needs.

        Names, descriptions and counts rather than whole configs: a list of ten
        self-contained configs is most of a megabyte of graphs nobody is looking
        at yet. A config that will not load is listed with the reason instead of
        omitted -- a file you cannot see is a file you cannot fix.
        """
        summaries: list[dict] = []
        for config_name in self.stored_config_names():
            try:
                config = self.load(config_name)
            except Exception as exc:  # noqa: BLE001 -- reported per entry, never fatal
                summaries.append({"name": config_name, "unreadable": str(exc)})
                continue
            summaries.append(
                {
                    "name": config.name,
                    "description": config.description,
                    "board": config.board,
                    "graph_names": [graph.name for graph in config.graphs],
                    "input_line_count": len(config.line_map.input_lines),
                    "output_line_count": len(config.line_map.output_lines),
                }
            )
        return summaries
