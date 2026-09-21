# SPDX-License-Identifier: LGPL-3.0-or-later
"""The gRPC channel, and the one place a transport failure becomes a refusal.

Nothing above this imports grpc, and nothing below it knows what a trial is.
The refusals themselves are `daemon_refusals.py`, because those are public and
this is not.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TypeVar

import grpc

from ._proto.statemachined.v1 import common_pb2
from .daemon_refusals import (
    DaemonIsUnavailable,
    DaemonRefusedTheRequest,
    NoBoardIsAttached,
    NoSuchDocument,
    TheRigIsNotInAStateForThat,
)

#: Where the daemon puts the refusal as itself. See
#: `daemon/src/statemachined/daemon/api/servicers/refusals.py`.
REFUSAL_METADATA_KEY = "statemachined-error-bin"

#: A `TypeVar` rather than PEP 695's `def call[T]`, because this package
#: supports Python 3.11 and that syntax arrived in 3.12 — the same reason the
#: daemon's `refusals.py` has one.
_T = TypeVar("_T")

#: The gRPC status a refusal came back with, and the class for it. Everything
#: else — `internal`, and any code this client has not heard of — is a plain
#: `DaemonRefusedTheRequest` with its `error` set, which is still catchable,
#: still readable, and still says what to change.
#:
#: `unavailable` is not in the table: it splits, and which way it goes depends
#: on whether anything answered. See :func:`refusal_of`.
_CLASS_FOR_STATUS = {
    "not_found": NoSuchDocument,
    "failed_precondition": TheRigIsNotInAStateForThat,
}


def refusal_of(error: grpc.RpcError) -> DaemonRefusedTheRequest:
    """A gRPC failure, as this package's refusal.

    A call that never landed — no daemon, a closed channel, a deadline — has no
    `statemachined.v1.Error` to carry, because nothing refused anything. It
    keeps the gRPC code and whatever the transport said.

    **`unavailable` is the one that splits**, and the split is the useful part:
    a typed refusal means the daemon answered and has no board, and no typed
    refusal means nothing answered at all. They read identically as a status
    code and the recovery for them is not the same.
    """
    status = error.code().name.lower()  # ty: ignore[unresolved-attribute]
    detail = error.details() or status  # ty: ignore[unresolved-attribute]
    body = _typed_refusal(error)
    if status == "unavailable":
        if body is None:
            return DaemonIsUnavailable(status, status, detail)
        return NoBoardIsAttached(status, body.error, body.detail, body.context)
    if body is None:
        body = common_pb2.Error(error=status, detail=detail)
    kind = _CLASS_FOR_STATUS.get(status, DaemonRefusedTheRequest)
    return kind(status, body.error, body.detail, body.context)


def _typed_refusal(error: grpc.RpcError) -> common_pb2.Error | None:
    for key, value in error.trailing_metadata() or ():  # ty: ignore[unresolved-attribute]
        if key == REFUSAL_METADATA_KEY:
            try:
                return common_pb2.Error.FromString(value)
            except Exception:
                return None  # a refusal we cannot read is still a refusal
    return None


def call(action: Callable[[], _T]) -> _T:
    """One rpc, with a gRPC failure turned into a refusal."""
    try:
        return action()
    except grpc.RpcError as error:
        raise refusal_of(error) from None


def stream(action: Callable[[], Iterator[_T]]) -> Iterator[_T]:
    """One server-streaming rpc.

    The refusal has to be raised from inside the iteration, because that is
    where gRPC raises it: a stream that fails after a thousand frames fails on
    the thousand-and-first `next`, not on the call that opened it.
    """
    try:
        yield from action()
    except grpc.RpcError as error:
        if error.code() is grpc.StatusCode.CANCELLED:  # ty: ignore[unresolved-attribute]
            return  # our own close, not a failure
        raise refusal_of(error) from None
