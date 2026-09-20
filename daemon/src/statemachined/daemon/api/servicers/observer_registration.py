# SPDX-License-Identifier: LGPL-3.0-or-later
"""Noting that somebody is watching, for the length of one stream.

`ReadObservers` answers a question a person asks out loud — *is triald still
listening?* — and it can only answer it if something registers. The WebSocket
routes did that inline, twice, with a `finally`. Under gRPC there are three
streams rather than two, so it is one context manager used three times.

**A diagnostic, not a contract.** The daemon does not act on this list, does
not wait for anybody in it, and does not remember it across a restart. It
publishes and assumes nobody read it; what the list is for is the question
somebody asks when trials stop reaching a consumer, which is otherwise answered
by guessing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from statemachined.daemon.api.rig_service import RigService

#: The metadata entry a client labels itself with. A header rather than a field
#: on every `Watch*Request`, because it says nothing about what is being asked
#: for — the same reason it was a query parameter and not a body.
OBSERVER_NAME_METADATA_KEY = "observer-name"


@contextmanager
def watching(service: RigService, context, *, stream: str) -> Iterator[int]:
    """Register this stream for as long as it runs, and give its id.

    The `finally` is the whole point: a gRPC stream ends by the generator being
    closed — a cancelled call, a client that went away, a daemon shutting down
    — and every one of those has to unregister. An observer list that only
    loses entries on a clean close fills up with ghosts, and then it answers
    the question wrongly rather than not at all, which is worse.
    """
    observer_id = service.observers.register(
        name=_declared_name(context),
        stream=stream,
        address=context.peer(),
    )
    try:
        yield observer_id
    finally:
        service.observers.unregister(observer_id)


def _declared_name(context) -> str | None:
    """What the client calls itself, or `None`. Unnamed is fine.

    The registry cleans it — control characters out, length capped — because
    this string comes off the wire and ends up on a screen.
    """
    for key, value in context.invocation_metadata() or ():
        if key == OBSERVER_NAME_METADATA_KEY:
            return value if isinstance(value, str) else value.decode("utf-8", "replace")
    return None
