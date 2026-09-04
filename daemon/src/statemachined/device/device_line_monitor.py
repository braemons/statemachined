# SPDX-License-Identifier: LGPL-3.0-or-later
"""Every line in and out of the port, as it went, kept for a while.

The thing a person reaches for when the layers above have stopped agreeing:
`/api/device/lines` says the valve is line 3 and the valve is not opening, so
what actually went down the wire? This answers that, in the protocol's own
words, with nothing interpreting them.

**Not the trace.** The trace (`state_visit_trace.py`) is the *record*: one entry
per state visit, timestamped in two clocks, written to disk, and joined to
triald's `.tdr` afterwards. This is a *log*: bytes, in order, thrown away when
the ring wraps, and interesting for about as long as somebody is watching it.
Keeping them apart is what stops the record filling with framing noise nobody
will read in six months.

**It is always on**, because the alternative is not. A link fault that happens
once an hour is not reproducible on demand, and a monitor somebody has to enable
first is a monitor that is off when the interesting thing happens. The cost is
one string appended per line -- a few hundred a second at worst, against a
bounded deque -- which is nothing next to the serial port it is describing.

**And it is only a tail.** `MONITORED_LINE_COUNT` lines, then the oldest go. A
consumer that falls behind is told what it missed, exactly as the trace's stream
does, rather than handed a shorter answer that looks complete.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

#: How many lines the ring holds. A graph set upload is a few hundred lines and
#: a heartbeat is two a second, so this is minutes of an idle link or one whole
#: upload with room around it -- which are the two things anybody scrolls back
#: to look at.
MONITORED_LINE_COUNT = 4000

#: Which way a line went. Spelled out rather than "tx"/"rx", because those are
#: only unambiguous once you have decided whose transmit it is.
TO_DEVICE = "to_device"
FROM_DEVICE = "from_device"


class DeviceLineMonitor:
    """The last few thousand lines, both directions, with their arrival times.

    Thread-safe: it is written from whichever thread holds the link -- the
    background reader, and any request thread sending a command -- and read from
    the API's threads. One lock, held across an append to a deque.
    """

    def __init__(self, ring_lines: int = MONITORED_LINE_COUNT):
        self._lines: deque[dict[str, Any]] = deque(maxlen=ring_lines)
        self._lock = threading.Lock()
        #: Monotonic for the life of the daemon, so a client can say where it
        #: got to. The same cursor the trace uses, for the same reason: a
        #: position in a ring is not a position in a log.
        self._next_entry_number = 0

    @property
    def ring_capacity(self) -> int:
        return self._lines.maxlen or 0

    def record(self, direction: str, line: str) -> dict[str, Any]:
        """One line, as it went. Never raises.

        Called from inside `SerialLink`, which is in the path of every command
        and of the scan-driven `visit` stream, so nothing here may fail: a
        monitor that could break the link would be worse than no monitor.
        """
        with self._lock:
            entry = {
                "entry_number": self._next_entry_number,
                "direction": direction,
                "line": line,
                "recorded_host_time": _iso8601_utc(time.time()),
            }
            self._next_entry_number += 1
            self._lines.append(entry)
            return entry

    # ----------------------------------------------------------- queries ---

    def lines_since(self, entry_number: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            return [line for line in self._lines if line["entry_number"] >= entry_number][:limit]

    def newest_entry_number(self) -> int:
        with self._lock:
            return self._next_entry_number - 1

    def oldest_entry_number_still_held(self) -> int:
        with self._lock:
            return self._lines[0]["entry_number"] if self._lines else self._next_entry_number

    def has_fallen_out_of_the_ring(self, entry_number: int) -> bool:
        with self._lock:
            if not self._lines:
                return False
            return entry_number < self._lines[0]["entry_number"]

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)


def _iso8601_utc(unix_seconds: float) -> str:
    seconds = int(unix_seconds)
    microseconds = round((unix_seconds - seconds) * 1_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{microseconds:06d}Z"
