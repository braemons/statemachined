# SPDX-License-Identifier: LGPL-3.0-or-later
"""Subscribing to a rig, and the two rules that make a subscription honest.

`WS /api/trace/stream` is how anything observes a statemachined rig. There is
nothing to register and nothing to be granted: **opening the socket is the whole
of subscribing and closing it is the whole of leaving**, the daemon publishes
and assumes nobody read it, and a rig with nobody watching runs and records
trials exactly the same. `?observer=<name>` puts a label next to the connection
on `GET /api/observers` so a person can see who is listening without reaching
for a packet capture; it grants nothing and there is nothing to forge.

Two rules live here because they are the two a caller gets wrong.

**The stream is not coalesced, and it can lose entries.** A consumer too slow
for the ring is sent `{"error": "fell_out_of_the_ring", ...}` and disconnected,
rather than handed a shorter answer that looks complete. That frame becomes
:class:`~statemachined.client.errors.TraceStreamLost` here -- an exception,
because a consumer that believed it saw everything is worse than one that knows
it did not -- and it is recoverable: `trace.for_trial(id)` answers exactly
whatever the stream did.

**The deadline is the subscriber's.** Nothing on the far end waits for a
subscriber, holds a trial for one, or retries. Only the side that knows a trial
is in flight can tell "not yet" from "never", and that side is never the rig.
So every wait in this module takes a timeout and none of them defaults to
forever.

The functions below take *messages*, not a socket, and that is deliberate: the
rule for which event ends a trial, and what a torn stream means, is then
testable against a list of strings and replayable against a log file. The socket
is :class:`TraceSubscription`, one layer up, and it is optional.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from typing import Any, Protocol

from .errors import TraceStreamLost

#: A state the machine entered, with how it was left and how long it took.
KIND_STATE_VISIT = "visit"

#: A trial ended. The one entry kind a trial loop acts on; everything else the
#: daemon publishes -- every state entered, every setting saved, every link
#: loss -- is for the record and for whoever else is watching.
KIND_TRIAL_RESULT = "trial_result"

#: What a subscriber that has not been given a name is called. A label on the
#: daemon's diagnostics page and nothing more.
DEFAULT_OBSERVER_NAME = "statemachined-client"


def is_a_finished_trial(entry: dict[str, Any]) -> bool:
    """Whether this published entry says a trial ended."""
    return entry.get("kind") == KIND_TRIAL_RESULT


def entries(messages: Iterable[str | bytes | dict]) -> Iterator[dict[str, Any]]:
    """Parse a stream of published messages, raising if it lost any.

    Accepts dicts as well as text so that a caller who has already decoded --
    or who is replaying a `.ndjson` recording through the same rule -- does not
    have to re-encode to use it.

    Raises:
        TraceStreamLost: on the daemon's `fell_out_of_the_ring` frame. Whatever
            follows it on that socket is not a continuation, so the iteration
            ends there rather than resuming.
    """
    for message in messages:
        entry = message if isinstance(message, dict) else json.loads(message)
        if "error" in entry:
            raise TraceStreamLost(
                int(entry.get("lost_from_entry_number", -1)),
                int(entry.get("lost_to_entry_number", -1)),
            )
        yield entry


def finished_trials(messages: Iterable[str | bytes | dict]) -> Iterator[int]:
    """The trial ids in a stream of published entries, as they finish."""
    for entry in entries(messages):
        if is_a_finished_trial(entry):
            yield int(entry["trial_id"])


class _Socket(Protocol):
    """The one method a subscription needs of whatever carries it.

    `websockets.sync.client.ClientConnection` satisfies it, and so does any
    adapter over another library or over a test client -- which is the point of
    naming it rather than importing a concrete type. A subscription is an
    iterator over received text; nothing here ever sends.
    """

    def recv(self, timeout: float | None = ...) -> str | bytes: ...

    def close(self) -> None: ...


class TraceSubscription:
    """One open subscription to a rig's trace.

    A context manager, because the socket is the subscription: leaving the block
    is how you unsubscribe, and there is nothing else to tell the daemon.

    ```python
    with client.trace.subscribe(observer="triald") as stream:
        for trial_id in stream.finished_trials(timeout_seconds=30):
            ...
    ```
    """

    def __init__(self, url: str, open_socket, receive_timeout_seconds: float | None = None):
        """
        Args:
            url: the `ws(s)://` address, query included.
            open_socket: a zero-argument callable returning a :class:`_Socket`.
                Injected rather than built here so that a test, or a caller
                using another websocket library, is not made to monkeypatch a
                module to reach the same rules.
            receive_timeout_seconds: the default deadline for a single receive.
                None waits as long as the socket will, which is right for a
                long-lived observer and wrong for anything driving a trial.
        """
        self.url = url
        self._open_socket = open_socket
        self._receive_timeout_seconds = receive_timeout_seconds
        self._socket: _Socket | None = None

    # ------------------------------------------------------------ lifetime ---

    def open(self) -> TraceSubscription:
        if self._socket is None:
            self._socket = self._open_socket()
        return self

    def close(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            socket.close()

    def __enter__(self) -> TraceSubscription:
        return self.open()

    def __exit__(self, *_exception) -> None:
        self.close()

    # ------------------------------------------------------------- reading ---

    def messages(self, timeout_seconds: float | None = None) -> Iterator[str | bytes]:
        """Raw frames, until the deadline passes or the socket closes.

        `timeout_seconds` is the budget for the *whole* iteration, not for one
        receive: a subscriber waiting for a trial to end has a deadline on the
        trial, and spending it one receive at a time is how a wait for one
        thing becomes an unbounded wait for many.
        """
        socket = self._require_open()
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        while True:
            if deadline is None:
                remaining = self._receive_timeout_seconds
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
            try:
                yield socket.recv(timeout=remaining) if remaining is not None else socket.recv()
            except TimeoutError:
                return
            except Exception as exc:
                # A closed socket ends the iteration rather than raising: the
                # daemon closing a stream is not a fault, it is what happens
                # when a rig restarts, and the caller's own deadline is what
                # decides whether that mattered.
                if _is_a_closed_socket(exc):
                    return
                raise

    def entries(self, timeout_seconds: float | None = None) -> Iterator[dict[str, Any]]:
        """Every published entry, in order, none skipped."""
        return entries(self.messages(timeout_seconds))

    def finished_trials(self, timeout_seconds: float | None = None) -> Iterator[int]:
        """The id of each trial as it ends."""
        return finished_trials(self.messages(timeout_seconds))

    def wait_for_trial(self, trial_id: int, timeout_seconds: float = 60.0) -> bool:
        """Block until that trial ends, or the deadline passes.

        Returns True if it ended, False if the deadline came first -- **not** an
        exception, because "not yet" is a state a caller has to be able to act
        on differently from "never", and only the caller knows which this was.

        Trials that end while waiting for another are skipped rather than
        raising: on a rig with one device that cannot happen, and on a replay it
        is not this call's business.
        """
        return any(finished == trial_id for finished in self.finished_trials(timeout_seconds))

    def _require_open(self) -> _Socket:
        if self._socket is None:
            raise RuntimeError(
                "the subscription is not open. Use it as a context manager, or call open()"
            )
        return self._socket


def _is_a_closed_socket(exception: Exception) -> bool:
    """Whether this exception is just the far end having gone away.

    Matched by name rather than by import so that this module does not depend
    on which websocket library the caller injected -- the whole reason
    :class:`_Socket` is a protocol.
    """
    return type(exception).__name__ in {
        "ConnectionClosed",
        "ConnectionClosedOK",
        "ConnectionClosedError",
        "WebSocketDisconnect",
        "StopIteration",
    }
