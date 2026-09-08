# SPDX-License-Identifier: LGPL-3.0-or-later
"""Who is watching this rig, while they are watching it.

**This daemon reports to nobody.** It publishes what it did -- every state a
trial entered, every result, every setting saved -- and assumes nobody read it.
Its own trace and recording exist on the rig whether anything is listening or
not, which is what makes a bench box with no other daemon on the network a
supported configuration rather than a broken one.

So an observer is not a subscriber this daemon serves. It is a *fact about
right now*: something opened a stream, said what it was, and has not gone away.
Nothing here can hold up a trial, nothing is retried, nothing is remembered
across a restart, and closing the socket is the whole of unregistering.

**Why keep the list at all**, when the daemon does not act on it: because "is
triald actually listening?" is the first question anybody asks when trials stop
being recorded, and without this the answer is a packet capture. A registry the
daemon never reads is still worth having if a person reads it.

A name is whatever the observer called itself in the query string. It is
**self-declared and unverified** -- there is no registration to forge and
nothing is granted by it, so a wrong name is a wrong label on a diagnostic
screen and nothing more. Shown as given.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: What an observer is called when it does not say. Not an error: a browser tab
#: on the web UI is an observer too, and it has nothing useful to declare.
UNNAMED = "unnamed"

#: The longest a self-declared name may be before it is cut. A name goes
#: straight onto a screen, so this is a display limit rather than a security
#: one -- there is nothing here to protect.
NAME_LIMIT = 64


@dataclass(frozen=True, slots=True)
class Observer:
    """One live stream, and what it has had from this daemon."""

    observer_id: int
    name: str
    stream: str
    """Which stream: `state` (coalesced) or `trace` (lossless)."""

    address: str | None
    """The peer's address as the server saw it, or None behind a socket that has none."""

    connected_at: float
    """`time.time()`, so a UI can say how long it has been watching."""

    delivered: int = 0
    """Messages sent to this observer. A counter that does not move is the tell."""

    fell_behind: bool = False
    """The trace ring passed this one by. It was told and disconnected."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "observer_id": self.observer_id,
            "name": self.name,
            "stream": self.stream,
            "address": self.address,
            "connected_at": self.connected_at,
            "connected_seconds": max(0.0, time.time() - self.connected_at),
            "delivered": self.delivered,
            "fell_behind": self.fell_behind,
        }


class ObserverRegistry:
    """The live streams, and nothing about the ones that have gone.

    Thread-safe because the streams are coroutines on the event loop while the
    link thread is what fills the trace they read; the lock is uncontended in
    practice and held for a dictionary write.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observers: dict[int, Observer] = {}
        self._next_id = itertools.count(1)

    def register(self, *, name: str | None, stream: str, address: str | None) -> int:
        """Take note of a stream that has just opened. Gives its id."""
        observer_id = next(self._next_id)
        with self._lock:
            self._observers[observer_id] = Observer(
                observer_id=observer_id,
                name=clean_name(name),
                stream=stream,
                address=address,
                connected_at=time.time(),
            )
        return observer_id

    def unregister(self, observer_id: int) -> None:
        """Forget it. Called from a `finally`, so it must not care if it is gone."""
        with self._lock:
            self._observers.pop(observer_id, None)

    def note_delivery(self, observer_id: int, messages: int = 1) -> None:
        """Count what an observer has been sent, so a stalled one is visible."""
        if messages <= 0:
            return
        with self._lock:
            observer = self._observers.get(observer_id)
            if observer is not None:
                self._observers[observer_id] = _replace(
                    observer, delivered=observer.delivered + messages
                )

    def note_fell_behind(self, observer_id: int) -> None:
        """It lost entries out of the ring and is about to be disconnected."""
        with self._lock:
            observer = self._observers.get(observer_id)
            if observer is not None:
                self._observers[observer_id] = _replace(observer, fell_behind=True)

    def observers(self) -> list[Observer]:
        """Everything watching now, oldest connection first."""
        with self._lock:
            return sorted(self._observers.values(), key=lambda o: o.connected_at)

    def __len__(self) -> int:
        with self._lock:
            return len(self._observers)


def clean_name(name: str | None) -> str:
    """A self-declared name, made safe to put on a screen.

    Control characters out and length capped: this string arrives from a query
    parameter and lands in a web UI, and neither the daemon nor the UI has any
    reason to render an observer's newlines.
    """
    if not name:
        return UNNAMED
    printable = "".join(character for character in name if character.isprintable())
    trimmed = printable.strip()[:NAME_LIMIT]
    return trimmed or UNNAMED


def _replace(observer: Observer, **changes: Any) -> Observer:
    """`dataclasses.replace` for a slotted frozen dataclass."""
    values = {
        "observer_id": observer.observer_id,
        "name": observer.name,
        "stream": observer.stream,
        "address": observer.address,
        "connected_at": observer.connected_at,
        "delivered": observer.delivered,
        "fell_behind": observer.fell_behind,
    }
    values.update(changes)
    return Observer(**values)
