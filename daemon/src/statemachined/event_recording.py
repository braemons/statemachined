# SPDX-License-Identifier: LGPL-3.0-or-later
"""A recording: a named, deliberate selection out of the always-on trace.

dev/API.md §9. The trace (`device/state_visit_trace.py`) is always on and always
bounded -- a ring plus a file per day -- because a link fault that happens once
an hour is not reproducible on demand and a monitor somebody has to switch on
first is one that is off when the interesting thing happens. That is the right
design for a *diagnostic* and the wrong one for a *record of an experiment*,
which needs three things the trace deliberately does not have: a name, a
boundary somebody chose, and a file that is only this run.

So a recording does not replace the trace or duplicate its rules. It is a sink
on it (`StateVisitTrace.add_sink`), which means it sees every entry **in order
and none skipped** -- exactly what the ring cannot promise a reader that fell
behind, and the reason this is a sink rather than something that polls
`/api/trace` and hopes.

**Four verbs, and they mean four different things.**

  * *start* opens a recording and its file.
  * *pause* stops capturing without ending the recording. The trace keeps
    running -- pausing a recording never blinds the rig -- so what a pause
    leaves behind is a **gap that is written down**: the segments below say
    which entry numbers are in and, by omission, which are not. A recording
    that quietly closed over its own gap would be the one thing worse than not
    having recorded at all.
  * *stop* ends it. The file stays; the recording is in the store.
  * *clear* throws away what has been captured **and keeps recording**. That is
    the verb for "the last ten minutes were me testing a valve" -- distinct
    from stop, which keeps what it has, and from deleting a stored recording,
    which is `DELETE /api/recordings/{name}`.

**Two files per recording, and the entries are one of them.** `<name>.ndjson`
is the entries, appended on arrival for the same reason the trace's day file is:
a crash otherwise loses exactly the window that mattered. `<name>.recording.json`
is the manifest -- the name, the segments, what is in it -- rewritten only when
one of the four verbs is used. So a daemon killed mid-recording leaves a
complete entries file and a manifest whose last segment has no end, which is a
true description of what happened and is how `manifests()` reports it.

**This is still not the `.tdr`.** triald writes the trial record and nothing in
here is a verdict. What this is for is the rig with no triald -- a bench, a
pilot, a training box -- where the daemon is the only thing that saw the session
happen.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

#: Same shape a state-machine config's name has, and for the same two reasons:
#: it is a file name, and a name with a `/` in it would be a path.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

ENTRIES_SUFFIX = ".ndjson"
MANIFEST_SUFFIX = ".recording.json"

STATE_RECORDING = "recording"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"


class RecordingNotInStore(RuntimeError):
    """A name that nothing on this rig answers to."""


class RecordingNameTaken(RuntimeError):
    """Refusing to record over a recording that already exists."""


class RecordingStateRefused(RuntimeError):
    """The verb does not apply to what is happening -- pause with nothing running."""


class BadRecordingName(ValueError):
    """A name that is not a name, which here also means not a file name."""


class EventRecorder:
    """The active recording, the stored ones, and the sink that fills them.

    Thread-safe: the sink runs on whichever thread owns the link and the verbs
    run on the API's request threads. The lock is held across a dict update and
    a line written to an open-append-close file -- both cheap, and neither able
    to block on the device.

    **Nothing here calls back into the trace.** The trace calls this, under its
    own lock; a recorder that appended to the trace while holding this lock
    would be two locks taken in two orders by two threads. The lifecycle entries
    are written by `RigService`, outside both.
    """

    def __init__(self, directory: Path | None = None):
        self._directory = Path(directory) if directory is not None else None
        self._lock = threading.Lock()
        #: The manifest of the recording being written, or None. Held in memory
        #: because it changes per entry -- rewriting the manifest file on every
        #: state visit would be a write per visit for a number nobody is
        #: reading between the verbs.
        self._active: dict[str, Any] | None = None

    # ------------------------------------------------------------- the sink ---

    def record(self, entry: dict[str, Any]) -> None:
        """One trace entry into the active recording, if one is capturing."""
        with self._lock:
            active = self._active
            if active is None or active["state"] != STATE_RECORDING:
                return
            segment = active["segments"][-1]
            if segment["from_entry_number"] is None:
                segment["from_entry_number"] = entry["entry_number"]
                segment["started_host_time"] = entry["recorded_host_time"]
            segment["to_entry_number"] = entry["entry_number"]
            segment["entry_count"] += 1
            active["entry_count"] += 1
            active["kind_counts"][entry["kind"]] = active["kind_counts"].get(entry["kind"], 0) + 1
            self._append_entry(active["name"], entry)

    # ---------------------------------------------------------- the verbs ---

    def start(
        self,
        name: str,
        description: str = "",
        state_machine_config: str | None = None,
    ) -> dict[str, Any]:
        """Open a recording. Refused if one is already open, or the name is taken.

        Refused rather than silently continuing the old one or silently
        overwriting the file: both of those are how a morning's recording turns
        out to be an afternoon's.
        """
        self._refuse_a_name_that_is_not_one(name)
        with self._lock:
            if self._active is not None:
                raise RecordingStateRefused(
                    f"{self._active['name']!r} is already {self._active['state']}. "
                    f"Stop it before starting another."
                )
            if self._manifest_path(name) is not None and self._manifest_path(name).exists():
                raise RecordingNameTaken(
                    f"a recording called {name!r} is already stored on this rig. "
                    f"Delete it or choose another name."
                )
            self._active = {
                "name": name,
                "description": description,
                "state_machine_config": state_machine_config,
                "created_unix_seconds": time.time(),
                "created_host_time": _iso8601_utc(time.time()),
                "state": STATE_RECORDING,
                "segments": [_a_new_segment()],
                "entry_count": 0,
                "kind_counts": {},
            }
            self._truncate_entries(name)
            self._write_manifest(self._active)
            return dict(self._active)

    def pause(self) -> dict[str, Any]:
        """Stop capturing; keep the recording open. The trace keeps running."""
        with self._lock:
            active = self._require_active()
            if active["state"] != STATE_RECORDING:
                raise RecordingStateRefused(f"{active['name']!r} is already paused")
            active["state"] = STATE_PAUSED
            self._close_the_open_segment(active)
            self._write_manifest(active)
            return dict(active)

    def resume(self) -> dict[str, Any]:
        with self._lock:
            active = self._require_active()
            if active["state"] != STATE_PAUSED:
                raise RecordingStateRefused(f"{active['name']!r} is not paused")
            active["state"] = STATE_RECORDING
            active["segments"].append(_a_new_segment())
            self._write_manifest(active)
            return dict(active)

    def stop(self) -> dict[str, Any]:
        """End the recording. The file stays and the recording is in the store."""
        with self._lock:
            active = self._require_active()
            active["state"] = STATE_STOPPED
            self._close_the_open_segment(active)
            active["stopped_host_time"] = _iso8601_utc(time.time())
            self._write_manifest(active)
            self._active = None
            return dict(active)

    def clear(self) -> dict[str, Any]:
        """Throw away what has been captured, and keep the recording open.

        The state is kept: clearing a paused recording leaves it paused, which
        is what somebody who paused it in order to clear it meant.
        """
        with self._lock:
            active = self._require_active()
            active["segments"] = [_a_new_segment()] if active["state"] == STATE_RECORDING else []
            active["entry_count"] = 0
            active["kind_counts"] = {}
            active["cleared_host_time"] = _iso8601_utc(time.time())
            self._truncate_entries(active["name"])
            self._write_manifest(active)
            return dict(active)

    def _require_active(self) -> dict[str, Any]:
        if self._active is None:
            raise RecordingStateRefused(
                "no recording is open on this rig. Start one, or -- to remove a stored "
                "recording -- delete it by name."
            )
        return self._active

    def _close_the_open_segment(self, active: dict[str, Any]) -> None:
        if not active["segments"]:
            return
        segment = active["segments"][-1]
        if segment["ended_host_time"] is None:
            segment["ended_host_time"] = _iso8601_utc(time.time())
        # A segment that caught nothing is dropped rather than stored as a pair
        # of nulls: it is a pause somebody took immediately, and a reader
        # counting gaps should not have to filter it out.
        if segment["from_entry_number"] is None:
            active["segments"].pop()

    # ------------------------------------------------------------ the store ---

    @property
    def active(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._active) if self._active is not None else None

    def manifest_of_the_active_recording(self) -> dict[str, Any]:
        """The open recording, or a refusal naming the verb that would open one."""
        with self._lock:
            return dict(self._require_active())

    def manifests(self) -> list[dict[str, Any]]:
        """Every recording on this rig, newest first, unreadable ones included.

        A manifest that will not parse is listed with its reason rather than
        omitted, for the same reason a broken graph is: a file you cannot see is
        a file you cannot fix.
        """
        if self._directory is None or not self._directory.exists():
            return []
        found = []
        for path in sorted(self._directory.glob(f"*{MANIFEST_SUFFIX}")):
            name = path.name[: -len(MANIFEST_SUFFIX)]
            try:
                found.append(self.manifest_of(name))
            except Exception as exc:  # noqa: BLE001
                found.append({"name": name, "unreadable": str(exc)})
        found.sort(key=lambda each: each.get("created_unix_seconds") or 0, reverse=True)
        return found

    def manifest_of(self, name: str) -> dict[str, Any]:
        """One recording's manifest -- the live one if it is the active recording.

        The active recording's counts are only in memory between the verbs, so
        reading it off disk would report the numbers as of the last start or
        pause. Whoever is watching a recording grow is watching this number.
        """
        self._refuse_a_name_that_is_not_one(name)
        with self._lock:
            if self._active is not None and self._active["name"] == name:
                return dict(self._active)
        path = self._manifest_path(name)
        if path is None or not path.exists():
            raise RecordingNotInStore(f"this rig has no recording called {name!r}")
        manifest = json.loads(path.read_text())
        manifest["entries_bytes"] = self._entries_bytes(name)
        return manifest

    def entries_of(self, name: str, offset: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """Read entries back out of the file, by position in it.

        By position rather than by entry number, because a recording with a gap
        in it has no contiguous range of entry numbers to slice -- the numbers
        are the trace's and the gaps are real. Each entry still carries its
        `entry_number`, so joining back to the trace is exact.
        """
        self._refuse_a_name_that_is_not_one(name)
        path = self._entries_path(name)
        if path is None or not path.exists():
            if self._manifest_path(name) is not None and self._manifest_path(name).exists():
                return []
            raise RecordingNotInStore(f"this rig has no recording called {name!r}")
        entries = []
        with path.open(encoding="utf-8") as file:
            for position, line in enumerate(file):
                if position < offset:
                    continue
                if len(entries) >= limit:
                    break
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    def delete(self, name: str) -> None:
        """Remove a stored recording. Refused while it is the one being written."""
        self._refuse_a_name_that_is_not_one(name)
        with self._lock:
            if self._active is not None and self._active["name"] == name:
                raise RecordingStateRefused(
                    f"{name!r} is being recorded right now. Stop it first -- deleting the file "
                    f"under a running recording would leave the rig writing into nothing."
                )
        manifest_path = self._manifest_path(name)
        if manifest_path is None or not manifest_path.exists():
            raise RecordingNotInStore(f"this rig has no recording called {name!r}")
        manifest_path.unlink()
        entries_path = self._entries_path(name)
        if entries_path is not None and entries_path.exists():
            entries_path.unlink()

    def suggest_a_name(self) -> str:
        """A name for somebody who did not bring one: the time it started.

        Sortable, unique in practice, and it says the one thing a recording
        nobody named still knows about itself.
        """
        return time.strftime("recording-%Y%m%d-%H%M%S", time.gmtime())

    # ------------------------------------------------------------- the files ---

    def _manifest_path(self, name: str) -> Path | None:
        return None if self._directory is None else self._directory / f"{name}{MANIFEST_SUFFIX}"

    def _entries_path(self, name: str) -> Path | None:
        return None if self._directory is None else self._directory / f"{name}{ENTRIES_SUFFIX}"

    def _entries_bytes(self, name: str) -> int:
        path = self._entries_path(name)
        return path.stat().st_size if path is not None and path.exists() else 0

    def _append_entry(self, name: str, entry: dict[str, Any]) -> None:
        path = self._entries_path(name)
        if path is None:
            return
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError as exc:
            # Same rule the trace has: a full disk must not stop a session. It
            # is written into the manifest rather than swallowed, because a
            # recording with a hole in it that says so is usable and one that
            # does not is a lie.
            if self._active is not None:
                self._active["write_failure"] = str(exc)

    def _truncate_entries(self, name: str) -> None:
        path = self._entries_path(name)
        if path is None:
            return
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        except OSError:
            pass

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        path = self._manifest_path(manifest["name"])
        if path is None:
            return
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".writing")
            temporary.write_text(json.dumps(manifest, indent=2) + "\n")
            temporary.replace(path)
        except OSError:
            pass

    @staticmethod
    def _refuse_a_name_that_is_not_one(name: str) -> None:
        if not NAME_PATTERN.match(name or ""):
            raise BadRecordingName(
                f"{name!r} is not a usable recording name. It becomes a file name, so it must "
                f"start with a letter or digit and hold only letters, digits, dot, dash and "
                f"underscore."
            )


def _a_new_segment() -> dict[str, Any]:
    return {
        "from_entry_number": None,
        "to_entry_number": None,
        "started_host_time": None,
        "ended_host_time": None,
        "entry_count": 0,
    }


def _iso8601_utc(unix_seconds: float) -> str:
    seconds = int(unix_seconds)
    microseconds = round((unix_seconds - seconds) * 1_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{microseconds:06d}Z"
