# SPDX-License-Identifier: GPL-3.0-or-later
"""The other transport, driven the way a browser drives it.

`web_edge.py` is the second way into the same servicers: a browser cannot speak
gRPC -- no access to HTTP trailers, no control over HTTP/2 framing -- so the
panels speak the Connect protocol over plain HTTP POST, and the edge translates.

**The failure this suite exists for.** A servicer is written against
`grpc.aio.ServicerContext`, and the edge supplies a stand-in that implements
"as much of it as a servicer actually uses". That is a promise, and it broke:
`observer_registration.watching` began calling `invocation_metadata()` and
`peer()`, `EdgeContext` had neither, and every stream a browser opened died
with an `AttributeError` -- while the gRPC path, which every integration suite
uses, was perfectly fine. Nothing in this repository could see it, because
nothing drove a stream through this transport.

So these are deliberately about the *seam* and not about what the rpcs answer:
the envelope framing, the refusal shape, and above all that the same servicer
object survives being called through both doors.
"""

from __future__ import annotations

import asyncio
import json
import struct

import pytest
from statemachined.daemon.api.rig_service import RigService
from statemachined.daemon.api.servicers import build_servicers
from statemachined.daemon.api.web_edge import EdgeContext, build_edge
from statemachined.daemon.rig_configuration import RigConfiguration

#: Connect's frame header: one flag byte, then four bytes of big-endian length.
#: `0b10` on the last frame marks it as the end-of-stream message rather than
#: another answer.
END_OF_STREAM = 0b10


@pytest.fixture
def rig(tmp_path):
    """A daemon with no board, never started. Nothing here reaches a device."""
    configuration = RigConfiguration(
        device_target="loop://",
        connect_on_startup=False,
        graph_store_directory=tmp_path / "graphs",
        state_machine_config_directory=tmp_path / "configs",
        trace_directory=tmp_path / "trace",
        recording_directory=tmp_path / "recordings",
    )
    return RigService(configuration)


@pytest.fixture
def edge(rig):
    return build_edge(rig, build_servicers(rig))


def post(
    edge,
    path: str,
    body: bytes,
    content_type: str,
    *,
    headers=(),
    frames: int = 0,
    while_open=None,
):
    """One POST against the ASGI edge, with an optional cap on frames read.

    `frames` is how many response frames to take before hanging up, because a
    subscription never ends on its own -- which is the property under test and
    also the thing that would hang this suite.

    `while_open` is called when the first frame arrives, which for a stream is
    the only moment the subscription is both registered and still there. It
    takes no arguments and whatever it returns comes back beside the body.
    """

    async def call():
        sent: list[dict] = []
        finished = asyncio.Event()
        delivered = False

        async def receive() -> dict:
            # The request body once, and then whatever the connection does.
            # Waiting for the second is what a browser holding a subscription
            # open looks like from the server's side — and a `receive` that
            # returned at once would end every stream on its first frame,
            # which would make these tests pass for the wrong reason.
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await finished.wait()
            return {"type": "http.disconnect"}

        noticed = None

        async def send(message: dict) -> None:
            nonlocal noticed
            sent.append(message)
            if message["type"] == "http.response.body":
                # On the first *frame* rather than on the response start: the
                # edge sends its headers before it touches the generator, so a
                # look then is a look before the servicer has done anything.
                if while_open is not None and noticed is None:
                    noticed = while_open()
            if message["type"] == "http.response.body" and frames:
                if sum(1 for m in sent if m["type"] == "http.response.body") >= frames:
                    finished.set()
                    raise _EnoughFrames

        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"content-type", content_type.encode()), *headers],
            "client": ("127.0.0.1", 54321),
        }
        try:
            await edge(scope, receive, send)
        except _EnoughFrames:
            pass
        start = next(m for m in sent if m["type"] == "http.response.start")
        chunks = [m.get("body", b"") for m in sent if m["type"] == "http.response.body"]
        if while_open is not None:
            return start["status"], b"".join(chunks), noticed
        return start["status"], b"".join(chunks)

    # A deadline, because the thing under test is a stream that does not end.
    return asyncio.run(asyncio.wait_for(call(), timeout=20))


class _EnoughFrames(Exception):
    """Our own hang-up, not a failure. See `post`."""


def unary(edge, path: str, body: dict | None = None, **kwargs):
    status, answer = post(
        edge, path, json.dumps(body or {}).encode(), "application/json", **kwargs
    )
    return status, json.loads(answer)


def enveloped(message: dict) -> bytes:
    """A streaming rpc's request, framed the way Connect frames one."""
    payload = json.dumps(message).encode()
    return struct.pack(">BI", 0, len(payload)) + payload


def unframe(body: bytes) -> list[tuple[int, dict]]:
    """Every `(flags, message)` in a Connect stream body."""
    frames = []
    offset = 0
    while offset + 5 <= len(body):
        flags, length = struct.unpack(">BI", body[offset : offset + 5])
        payload = body[offset + 5 : offset + 5 + length]
        frames.append((flags, json.loads(payload) if payload else {}))
        offset += 5 + length
    return frames


# -- the context ----------------------------------------------------------------


def test_the_stand_in_context_answers_everything_a_servicer_asks_of_it():
    """The promise `EdgeContext` makes, checked against the servicers.

    Written out rather than duck-typed: a servicer reaching for a method this
    has not got fails at the moment a panel opens a stream, which is the one
    place in this daemon nothing else looks.
    """
    context = EdgeContext({"headers": [], "client": ("127.0.0.1", 1)})
    for method in ("abort", "invocation_metadata", "peer"):
        assert callable(getattr(context, method, None)), f"a servicer calls {method}()"


def test_a_header_is_metadata_the_way_grpc_spells_it():
    """A browser cannot set arbitrary gRPC metadata and can set a header, which
    is how a panel names itself in the observer list exactly as a client does."""
    context = EdgeContext({"headers": [(b"Observer-Name", b"the-trace-panel")]})
    assert ("observer-name", "the-trace-panel") in context.invocation_metadata()


def test_a_caller_with_no_address_is_not_a_crash():
    assert EdgeContext({}).peer() == "unknown"


# -- unary ----------------------------------------------------------------------


def test_a_unary_rpc_answers_json(edge):
    status, answer = unary(edge, "/statemachined.v1.Configuration/ReadHealth")
    assert status == 200
    assert answer == {"ok": True, "device_connected": False}


def test_a_refusal_carries_the_daemons_own_words(edge):
    """The refusal shape is the whole reason this transport is hand-written:
    Connect puts the typed `Error` in `details`, base64, because a browser
    cannot read a gRPC trailer."""
    status, answer = unary(
        edge, "/statemachined.v1.GraphStore/ReadGraphFile", {"name": "not-there"}
    )
    assert status == 404
    assert answer["code"] == "not_found"
    assert "not-there" in answer["message"]
    assert answer.get("details"), "the typed refusal travels as itself"


def test_an_rpc_that_does_not_exist_is_unimplemented_and_not_a_traceback(edge):
    status, answer = unary(edge, "/statemachined.v1.State/NoSuchRpc")
    assert status == 404
    assert answer["code"] == "unimplemented"


def test_a_content_type_this_edge_does_not_speak_says_which_it_does(edge):
    status, answer = post(edge, "/statemachined.v1.Configuration/ReadHealth", b"{}", "text/plain")
    assert status == 415
    assert "application/json" in json.loads(answer)["message"]


# -- streaming ------------------------------------------------------------------


def test_a_stream_delivers_frames_through_this_transport(edge):
    """**The test that was missing.** Every integration suite goes over gRPC;
    this is the only thing in the repository that opens a stream the way the
    panels do, and it is what would have caught `EdgeContext` losing a method.
    """
    status, body = post(
        edge,
        "/statemachined.v1.State/WatchState",
        enveloped({}),
        "application/connect+json",
        frames=1,
    )
    assert status == 200
    frames = unframe(body)
    assert frames, "the subscription delivered nothing at all"
    flags, first = frames[0]
    assert flags == 0, "an answer, not the end of the stream"
    assert first["state"]["connected"] is False


def test_a_subscriber_shows_up_in_the_observer_list_by_the_name_it_sent(edge, rig):
    """A panel names itself with a header, and the daemon reads it the same way
    it reads a client's metadata. **This is the call path that was broken.**

    Looked at while the stream is open, because hanging up is the whole of
    unsubscribing — a check afterwards would find an empty list whether or not
    anything was ever registered, which is the way this test would pass for the
    wrong reason.
    """
    _status, _body, watching = post(
        edge,
        "/statemachined.v1.State/WatchState",
        enveloped({}),
        "application/connect+json",
        headers=[(b"observer-name", b"the-trace-panel")],
        frames=1,
        while_open=lambda: [observer.as_dict() for observer in rig.observers.observers()],
    )

    assert watching, "nothing registered; a servicer asked the context for something"
    assert watching[0]["name"] == "the-trace-panel"
    assert watching[0]["stream"] == "state"
    assert "127.0.0.1" in watching[0]["address"], "a browser has an address too"

    assert len(rig.observers) == 0, "and hanging up is the whole of unsubscribing"


def test_a_stream_that_is_hung_up_on_does_not_take_the_daemon_with_it(edge):
    """A browser tab closing is the ordinary case, not an error.

    Whatever the generator was doing is cancelled, and the next call has to
    work — which is what this asserts by making one.
    """
    post(
        edge,
        "/statemachined.v1.State/WatchState",
        enveloped({}),
        "application/connect+json",
        frames=1,
    )
    status, answer = unary(edge, "/statemachined.v1.Configuration/ReadHealth")
    assert status == 200 and answer["ok"] is True
