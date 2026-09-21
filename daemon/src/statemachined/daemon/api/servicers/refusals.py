# SPDX-License-Identifier: AGPL-3.0-or-later
"""One refusal shape, for every rpc.

The domain raises its own exceptions — `GraphNotInStore`, `NoConfigLoaded`,
`DeviceRefusedTheCommand` — and this is the single place each becomes a gRPC
status. The routes did the same job with `HTTPException`; the difference is
that this table is one table rather than a `raise refusal(404, ...)` repeated
at forty call sites, each free to pick a different number for the same thing.

**The codes are chosen for what they mean**, not transcribed from the HTTP
status the routes answered with:

* `unavailable` — no board. Nothing about the request is wrong and it will work
  when the link comes back. **The one a caller may retry unchanged.**
* `failed_precondition` — the daemon is not in a state where this means
  anything: no config loaded, a trial already running, a recording not open.
* `invalid_argument` — understood and refused: a graph that does not compile, a
  name that does not match its file, a line map this board cannot honour.
* `not_found` — no such graph, config or recording.
* `internal` — this daemon broke, not the caller.

**The whole refusal also travels as itself.** A status code is a category and a
message is a sentence; `error` is the part a client switches on and `context`
names what to change. So `statemachined.v1.Error` is encoded into the trailing
metadata entry `statemachined-error-bin`, which is what makes `common.proto`
the one description of a refusal rather than a shape nothing sends. The message
keeps `detail (context)` for everything that has not been told about the
metadata: grpcurl, a log line, a failed test.

**A device's refusal is passed up with the device's own words.** When the board
says `graph_mismatch` or `busy`, `error` is that code rather than this daemon's
paraphrase: those are the words its documentation uses, and a caller switching
on them should be switching on the device's answer.
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import Callable
from typing import TypeVar

import grpc

from statemachined._proto.statemachined.v1 import common_pb2
from statemachined.daemon.api.convert.trial import Refused as ConversionRefused
from statemachined.daemon.api.rig_service import (
    GraphNotInTheLoadedConfig,
    NoActiveGraph,
    NoConfigLoaded,
)
from statemachined.daemon.event_recording import (
    ARecordingIsAlreadyOpen,
    BadRecordingName,
    RecordingIsInProgress,
    RecordingNameTaken,
    RecordingNotInStore,
    RecordingStateRefused,
)
from statemachined.daemon.graph_store import GraphNameMismatch, GraphNotInStore
from statemachined.daemon.state_machine_config_store import (
    ConfigNameMismatch,
    ConfigNotInStore,
)
from statemachined.device.message_framing import DeviceRefusedTheCommand
from statemachined.model.line_map import LineMapDoesNotMatchTheBoard
from statemachined.device.statemachined_device import DeviceNotConnected, NoGraphSetCommitted
from statemachined.graph_set_compiler import GraphNotInSet, GraphSetCompilationError

#: Where the typed refusal rides. `-bin` is gRPC's own spelling for a metadata
#: value that is bytes rather than ASCII, which is what lets `detail` hold a
#: graph name with a non-ASCII character in it.
REFUSAL_METADATA_KEY = "statemachined-error-bin"

#: A `TypeVar` rather than PEP 695's `def answering[T]`, because this package
#: still supports Python 3.11 and that syntax arrived in 3.12.
_T = TypeVar("_T")


class Category(enum.Enum):
    """Why a call was refused, in this daemon's own words.

    Deliberately **not** an HTTP status. The routes carried numbers from a
    protocol this API no longer speaks, and a domain layer that knows `409`
    knows something that means nothing to it.
    """

    NO_BOARD_ATTACHED = "no_board_attached"
    WRONG_MOMENT = "wrong_moment"
    BAD_REQUEST = "bad_request"
    NO_SUCH_THING = "no_such_thing"
    THE_DAEMON_BROKE = "the_daemon_broke"


_CODE_FOR_CATEGORY = {
    Category.NO_BOARD_ATTACHED: grpc.StatusCode.UNAVAILABLE,
    Category.WRONG_MOMENT: grpc.StatusCode.FAILED_PRECONDITION,
    Category.BAD_REQUEST: grpc.StatusCode.INVALID_ARGUMENT,
    Category.NO_SUCH_THING: grpc.StatusCode.NOT_FOUND,
    Category.THE_DAEMON_BROKE: grpc.StatusCode.INTERNAL,
}


class Refusal(Exception):
    """A call this daemon will not make, with everything a caller needs.

    Raised directly where a servicer decides, and produced by `refusal_for`
    from whatever the domain raised.
    """

    def __init__(self, category: Category, error: str, detail: str, context: str = "") -> None:
        super().__init__(detail)
        self.category = category
        self.error = error
        self.detail = detail
        #: Never empty: where there is nothing more specific it repeats `error`,
        #: because "which field" with no answer is worse than a coarse one.
        self.context = context or error


def no_board_attached(detail: str = "no board is connected") -> Refusal:
    return Refusal(Category.NO_BOARD_ATTACHED, "not_connected", detail, "device")


#: Every domain exception this daemon raises, and what it means on the wire.
#:
#: Exhaustive by intent rather than by construction — Python has no way to
#: enumerate every exception a package can raise — so `refusal_for` falls back
#: to `internal` and `tests/unit/test_refusals.py` holds this list to the ones
#: the domain actually defines.
_RULES: list[tuple[type[BaseException], Category, str, str]] = [
    (GraphNotInStore, Category.NO_SUCH_THING, "no_such_graph", "graph_name"),
    (ConfigNotInStore, Category.NO_SUCH_THING, "no_such_state_machine_config", "config_name"),
    (RecordingNotInStore, Category.NO_SUCH_THING, "no_such_recording", "name"),
    (GraphNameMismatch, Category.BAD_REQUEST, "graph_name_mismatch", "graph_name"),
    (ConfigNameMismatch, Category.BAD_REQUEST, "config_name_mismatch", "config_name"),
    (BadRecordingName, Category.BAD_REQUEST, "bad_recording_name", "name"),
    (RecordingNameTaken, Category.BAD_REQUEST, "recording_name_taken", "name"),
    # Before `GraphSetCompilationError`, which it subclasses: the table is
    # walked in order and the first match wins, so the specific one has to
    # come first. "This set has no graph called X" and "this set does not fit
    # on the board" are different things to fix.
    (GraphNotInSet, Category.WRONG_MOMENT, "graph_not_in_set", "graph"),
    (GraphSetCompilationError, Category.BAD_REQUEST, "does_not_fit", "graphs"),
    # Before `RecordingStateRefused`, which it subclasses: the table is walked
    # in order. "Stop it first" and "there is nothing open" are different
    # things to do, and `error` is what a caller branches on.
    (RecordingIsInProgress, Category.WRONG_MOMENT, "recording_in_progress", "name"),
    (ARecordingIsAlreadyOpen, Category.WRONG_MOMENT, "already_recording", "recording"),
    (RecordingStateRefused, Category.WRONG_MOMENT, "recording_state", "recording"),
    (
        NoConfigLoaded,
        Category.WRONG_MOMENT,
        "no_state_machine_config_loaded",
        "state_machine_config",
    ),
    (NoActiveGraph, Category.WRONG_MOMENT, "no_graph_named", "graph"),
    # Selected a graph the loaded config does not carry. Caught at selection
    # rather than at the moment somebody presses run, which is the difference
    # between a refusal and a rig that looks armed.
    (GraphNotInTheLoadedConfig, Category.WRONG_MOMENT, "graph_not_available", "graph"),
    # A config, or a line map, naming a pin this board has not got. Refused
    # with the rig still running on the map it had — which is the difference
    # between a refusal and a rig that has been half-reconfigured.
    (
        LineMapDoesNotMatchTheBoard,
        Category.BAD_REQUEST,
        "line_map_does_not_match_the_board",
        "line_map",
    ),
    (NoGraphSetCommitted, Category.WRONG_MOMENT, "no_graph_set", "graph"),
    (DeviceNotConnected, Category.NO_BOARD_ATTACHED, "not_connected", "device"),
]


def refusal_for(exception: BaseException) -> Refusal:
    """Whatever the domain raised, as a refusal.

    `DeviceRefusedTheCommand` is first and separate because it carries the
    board's own code and context, and flattening those into a generic
    `failed_precondition` would throw away the half a caller acts on.
    """
    if isinstance(exception, Refusal):
        return exception
    if isinstance(exception, DeviceRefusedTheCommand):
        return Refusal(
            Category.WRONG_MOMENT,
            exception.code,
            exception.message or str(exception),
            exception.context,
        )
    if isinstance(exception, ConversionRefused):
        return Refusal(Category.BAD_REQUEST, "bad_request", exception.detail, exception.context)
    for kind, category, error, context in _RULES:
        if isinstance(exception, kind):
            return Refusal(category, error, _sentence(exception), context)
    return Refusal(
        Category.THE_DAEMON_BROKE,
        "internal",
        _sentence(exception) or exception.__class__.__name__,
    )


def _sentence(exception: BaseException) -> str:
    """The message the domain wrote, without `KeyError`'s quotes around it.

    `str(KeyError("no graph called 'nope' is stored"))` is that sentence with a
    repr's quotes wrapped around it — because `KeyError` is meant to print a
    key, not a sentence. `GraphNotInStore` and `ConfigNotInStore` are
    `KeyError`s so that a caller inside the daemon can treat them as lookups,
    and their messages are sentences written to be read by a person. Taking the
    argument directly is what keeps both true.
    """
    if isinstance(exception, KeyError) and len(exception.args) == 1:
        return str(exception.args[0])
    return str(exception)


def _code_for(category: Category) -> grpc.StatusCode:
    code = _CODE_FOR_CATEGORY.get(category)
    if code is None:  # pragma: no cover - a new Category with no code chosen
        raise AssertionError(f"no gRPC status chosen for {category}")
    return code


async def refuse(context, refusal: Refusal):
    """Abort the call with the refusal, in full.

    `abort` never returns — it raises — but it is awaited and typed as though
    it might, so callers `raise` the result to make the control flow visible.
    """
    message = f"{refusal.detail} ({refusal.context})" if refusal.context else refusal.detail
    await context.abort(
        _code_for(refusal.category),
        message,
        trailing_metadata=(
            (
                REFUSAL_METADATA_KEY,
                common_pb2.Error(
                    error=refusal.error, detail=refusal.detail, context=refusal.context
                ).SerializeToString(),
            ),
        ),
    )


async def answering(context, work: Callable[[], _T]) -> _T:
    """Run one rpc's body on a thread, turning whatever it raises into a status.

    Two jobs, and the second is easy to miss.

    **Every exception becomes a status.** One raised six frames down in the
    device layer reaches the caller as the refusal it is rather than as an
    `INTERNAL` with a traceback in the log — and adding a servicer does not
    mean remembering what to catch.

    **`work` is synchronous, and runs off the event loop.** `RigService` is
    threads and locks over a serial port: `configure_trial` waits for the board
    to answer, and `open_session` waits for a whole graph set to upload. Called
    directly from a coroutine, each of those would stall *every* other rpc on
    the same loop — the state stream a console is watching, the trace triald is
    subscribed to — for as long as the port took. On a rig that is a UI that
    freezes whenever a trial arms.

    The routes had this for free: Starlette runs a `def` handler in a
    threadpool. Doing it here, once, is what keeps that property while the
    transport changes.
    """
    try:
        return await asyncio.to_thread(work)
    except Exception as exception:
        await refuse(context, refusal_for(exception))
        raise  # unreachable: abort() raises. Here so the type is honest.


async def reading(work: Callable[[], _T]) -> _T:
    """One blocking read, off the event loop, with no refusal wrapper.

    For the streams, whose bodies are `async def` generators rather than rpc
    bodies: a generator that called `read_device_state()` directly would block
    the loop on every frame, which is the same fault as above arriving once per
    tick instead of once per call.
    """
    return await asyncio.to_thread(work)
