# SPDX-License-Identifier: LGPL-3.0-or-later
"""The trace: every state the machine entered, timestamped, kept regardless.

dev/DAEMON.md §4.6. This is the one job that is *only* possible in a daemon: it
has to be running and listening at the moment a state is entered, which no
library invoked per trial is.

**A ring in memory is what the API reads.** That is what makes the rest of this
small: there is no "which file holds trial 193", no seek, no per-day file to
select, no index. A query is a scan of a deque.

**And a file it never reads back.** The ring is volatile and a `systemctl
restart` during a package upgrade must not silently cost the morning's traces,
so each entry is also appended to NDJSON -- one file per day, rotated by
logrotate. Appended **on arrival, not on eviction**, which is the whole
difference: a crash otherwise loses exactly the window that mattered most, which
is everything still in the ring.

NDJSON rather than SQLite for the same reason the wire is NDJSON: append-only,
so a crash mid-write costs the last line and not the file; greppable on the rig
at 2 a.m. with no tooling; and it copies.

**This is not the `.tdr` and must not grow into one.** triald writes the trial
record. This is finer grained -- one line per state visit rather than one per
trial -- and it joins to the `.tdr` on `trial_id`, which is the entire reason
`trial_id` is on the wire. Nothing in it is a verdict.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

#: Entry kinds. The device is not the only thing worth timestamping: aligning an
#: external signal to trial 193 needs to know when trial 193 was *armed*, not
#: only which states it visited, and splitting that across two streams means
#: joining them later.
KIND_STATE_VISIT = "visit"
KIND_TRIAL_CONFIGURED = "trial_configured"
KIND_TRIAL_STARTED = "trial_started"
KIND_TRIAL_CANCELLED = "trial_cancelled"
KIND_TRIAL_RESULT = "trial_result"
KIND_GRAPH_SET_UPLOADED = "graph_set_uploaded"
KIND_LINK_CONNECTED = "link_connected"
KIND_LINK_LOST = "link_lost"
KIND_SEQUENCE_GAP = "sequence_gap"


class StateVisitTrace:
    """The ring, the file, and the entry numbers that address them.

    Thread-safe because it is written from whichever thread owns the link and
    read from the API's request threads. One lock, held only across a list
    append and a file write -- both cheap, and neither able to block on the
    device.
    """

    def __init__(self, ring_entries: int, directory: Path | None = None):
        self._entries: deque[dict[str, Any]] = deque(maxlen=ring_entries)
        self._directory = Path(directory) if directory is not None else None
        self._lock = threading.Lock()
        #: Monotonic for the life of the daemon. The device's `seq` counts
        #: visits within a *run* and restarts at zero every trial, so it cannot
        #: address a position in a log that spans a session -- which is what a
        #: `since_` cursor needs. Both are carried; this one is the cursor.
        self._next_entry_number = 0

    @property
    def ring_capacity(self) -> int:
        return self._entries.maxlen or 0

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Add one entry, to the ring and to the day's file.

        Returns it, with its `entry_number` and the host time it was recorded
        at -- which is the daemon's own clock and is *not* an estimate of the
        device's, unlike a visit's `entered_host_time`.
        """
        with self._lock:
            entry = {
                "entry_number": self._next_entry_number,
                "kind": kind,
                "recorded_host_time": _iso8601_utc(time.time()),
                **fields,
            }
            self._next_entry_number += 1
            self._entries.append(entry)
            self._append_to_todays_file(entry)
            return entry

    def _append_to_todays_file(self, entry: dict[str, Any]) -> None:
        if self._directory is None:
            return
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            day = time.strftime("%Y-%m-%d", time.gmtime())
            with (self._directory / f"trace-{day}.ndjson").open("a", encoding="utf-8") as file:
                file.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError:
            # A full or read-only disk must not stop a session. The ring is
            # what the API reads, so the trace survives this; what is lost is
            # the durable copy, and losing a session over it would be worse.
            pass

    # ----------------------------------------------------------- queries ---

    def entries_since(self, entry_number: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            return [
                entry for entry in self._entries if entry["entry_number"] >= entry_number
            ][:limit]

    def entries_for_trial(self, trial_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [entry for entry in self._entries if entry.get("trial_id") == trial_id]

    def newest_entry_number(self) -> int:
        with self._lock:
            return self._next_entry_number - 1

    def oldest_entry_number_still_held(self) -> int:
        with self._lock:
            return self._entries[0]["entry_number"] if self._entries else self._next_entry_number

    def has_fallen_out_of_the_ring(self, entry_number: int) -> bool:
        """Whether a cursor is older than anything still held.

        A consumer slow enough for this has genuinely lost data and must be
        **told so**, with the range that is gone, rather than handed a shorter
        answer that looks complete. It is the one place the ring's boundedness
        is visible from outside, and it beats the alternative: buffering per
        client is how a monitoring aid becomes the thing that fills the Pi's
        memory.
        """
        with self._lock:
            if not self._entries:
                return False
            return entry_number < self._entries[0]["entry_number"]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _iso8601_utc(unix_seconds: float) -> str:
    seconds = int(unix_seconds)
    microseconds = round((unix_seconds - seconds) * 1_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{microseconds:06d}Z"
