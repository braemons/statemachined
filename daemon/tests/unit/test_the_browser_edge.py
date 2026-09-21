# SPDX-License-Identifier: GPL-3.0-or-later
"""The other transport, driven the way a browser drives it.

`web_edge.py` is the second way into the same servicers: a browser cannot speak
gRPC -- no access to HTTP trailers, no control over HTTP/2 framing -- so the
panels speak gRPC-Web over plain HTTP POST, and the edge translates.

**The failure this suite exists for.** A servicer is written against
`grpc.aio.ServicerContext`, and the edge supplies a stand-in that implements
"as much of it as a servicer actually uses". That is a promise, and it broke:
`observer_registration.watching` began calling `invocation_metadata()` and
`peer()`, `EdgeContext` had neither, and every stream a browser opened died
with an `AttributeError` -- while the gRPC path, which every integration suite
uses, was perfectly fine. Nothing in this repository could see it, because
nothing drove a stream through this transport.

So these are deliberately about the *seam* and not about what the rpcs answer:
the framing, the trailer that carries the outcome, and above all that the same
servicer object survives being called through both doors.
"""

from __future__ import annotations

import asyncio
import base64
import struct
from urllib.parse import unquote

import grpc
import pytest
from google.protobuf.message_factory import GetMessageClass
from statemachined._proto.statemachined.v1 import common_pb2, service_pb2
from statemachined.daemon.api.rig_service import RigService
from statemachined.daemon.api.servicers import build_servicers
from statemachined.daemon.api.servicers.refusals import REFUSAL_METADATA_KEY
from statemachined.daemon.api.web_edge import EdgeContext, build_edge
from statemachined.daemon.rig_configuration import RigConfiguration

#: gRPC-Web's frame header: one flag byte, then four bytes of big-endian
#: length. Bit 7 on the last frame marks it as the trailer -- the outcome of
#: the call -- rather than another answer.
TRAILER = 0x80

#: The one content type the edge speaks.
GRPC_WEB = "application/grpc-web+proto"


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


def request_for(path: str):
    """The request message an rpc takes, from the descriptor rather than by name."""
    service, method = path.lstrip("/").split("/")
    descriptor = service_pb2.DESCRIPTOR.services_by_name[service.split(".")[-1]]
    return GetMessageClass(descriptor.methods_by_name[method].input_type)


def answer_for(path: str):
    """The answer message an rpc gives, likewise."""
    service, method = path.lstrip("/").split("/")
    descriptor = service_pb2.DESCRIPTOR.services_by_name[service.split(".")[-1]]
    return GetMessageClass(descriptor.methods_by_name[method].output_type)


def framed(message: bytes) -> bytes:
    """A request, framed the way gRPC-Web frames one."""
    return struct.pack(">BI", 0, len(message)) + message


def request(path: str, fields: dict | None = None) -> bytes:
    """A framed request message for `path`, built from the descriptor."""
    message = request_for(path)()
    for name, value in (fields or {}).items():
        setattr(message, name, value)
    return framed(message.SerializeToString())


def unframe(body: bytes) -> list[tuple[int, bytes]]:
    """Every `(flags, payload)` in a gRPC-Web body."""
    frames = []
    offset = 0
    while offset + 5 <= len(body):
        flags, length = struct.unpack(">BI", body[offset : offset + 5])
        frames.append((flags, body[offset + 5 : offset + 5 + length]))
        offset += 5 + length
    return frames


def trailer_of(body: bytes) -> dict[str, str]:
    """The trailer frame's `key: value` lines, as a mapping.

    `grpc-message` comes back decoded: it is percent-encoded on the wire so
    that a refusal's sentence can carry any byte a person wrote.
    """
    for flags, payload in unframe(body):
        if flags & TRAILER:
            trailer = {}
            for line in payload.decode().split("\r\n"):
                if ":" in line:
                    key, _, value = line.partition(":")
                    trailer[key.strip().lower()] = value.strip()
            if "grpc-message" in trailer:
                trailer["grpc-message"] = unquote(trailer["grpc-message"])
            return trailer
    raise AssertionError("no trailer frame; the call never said how it went")


def answers_of(path: str, body: bytes) -> list:
    """Every answer message in a body, decoded against the rpc's output type."""
    schema = answer_for(path)
    messages = []
    for flags, payload in unframe(body):
        if not flags & TRAILER:
            message = schema()
            message.ParseFromString(payload)
            messages.append(message)
    return messages


def request_of_nothing() -> bytes:
    """A framed empty message, for a path with no descriptor to look up."""
    return framed(b"")


def unary(edge, path: str, fields: dict | None = None, **kwargs):
    """One unary call. Returns the HTTP status, the trailer, and the answers."""
    status, body = post(edge, path, request(path, fields), GRPC_WEB, **kwargs)
    return status, trailer_of(body), answers_of(path, body)


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


def test_a_unary_rpc_answers_a_framed_message(edge):
    status, trailer, answers = unary(edge, "/statemachined.v1.Configuration/ReadHealth")
    assert status == 200
    assert trailer["grpc-status"] == "0"
    assert len(answers) == 1
    assert answers[0].ok is True
    assert answers[0].device_connected is False


def test_a_refusal_keeps_its_http_200_and_says_so_in_the_trailer(edge):
    """**The shape of every outcome in this protocol.** gRPC puts the status in
    HTTP trailers, which a browser cannot read; gRPC-Web puts it in the last
    frame of the body, which it can. The status line is 200 either way, because
    by the time a stream fails it is long gone."""
    status, trailer, answers = unary(
        edge, "/statemachined.v1.GraphStore/ReadGraphFile", {"name": "not-there"}
    )
    assert status == 200, "the outcome is in the trailer, not the status line"
    assert trailer["grpc-status"] == str(grpc.StatusCode.NOT_FOUND.value[0])
    assert "not-there" in trailer["grpc-message"]
    assert not answers, "a refusal answers nothing"


def test_the_typed_refusal_travels_as_itself(edge):
    """The same `statemachined.v1.Error` the gRPC transport puts in trailing
    metadata, so a browser and a Python client read the same three fields and
    neither has to parse a sentence.

    Unpadded base64, which is gRPC's rule for a `-bin` value and not the padded
    base64 the Connect protocol used. Decoding it here is what says the browser
    can.
    """
    _status, trailer, _answers = unary(
        edge, "/statemachined.v1.GraphStore/ReadGraphFile", {"name": "not-there"}
    )
    encoded = trailer[REFUSAL_METADATA_KEY]
    assert not encoded.endswith("="), "a -bin value is unpadded"
    refusal = common_pb2.Error()
    refusal.ParseFromString(base64.b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert refusal.error == "no_such_graph", "the machine-readable code, not the sentence"
    assert refusal.context == "graph_name", "and what to change, which a status code cannot say"
    assert "not-there" in refusal.detail


def test_an_rpc_that_does_not_exist_is_unimplemented_in_the_trailer(edge):
    """Answered the way a refusal is, so a client needs one way to read an
    outcome rather than two."""
    status, body = post(edge, "/statemachined.v1.State/NoSuchRpc", request_of_nothing(), GRPC_WEB)
    assert status == 200
    trailer = trailer_of(body)
    assert trailer["grpc-status"] == str(grpc.StatusCode.UNIMPLEMENTED.value[0])
    assert "NoSuchRpc" in trailer["grpc-message"]


def test_a_content_type_this_edge_does_not_speak_says_which_it_does(edge):
    """415, not a trailer: a caller sending the wrong content type has not made
    a gRPC request for this edge to answer."""
    status, answer = post(
        edge, "/statemachined.v1.Configuration/ReadHealth", request_of_nothing(), "text/plain"
    )
    assert status == 415
    assert GRPC_WEB in answer.decode()


# -- streaming ------------------------------------------------------------------


def test_a_stream_delivers_frames_through_this_transport(edge):
    """**The test that was missing.** Every integration suite goes over gRPC;
    this is the only thing in the repository that opens a stream the way the
    panels do, and it is what would have caught `EdgeContext` losing a method.
    """
    path = "/statemachined.v1.State/WatchState"
    status, body = post(edge, path, request(path), GRPC_WEB, frames=1)
    assert status == 200
    frames = unframe(body)
    assert frames, "the subscription delivered nothing at all"
    flags, _payload = frames[0]
    assert not flags & TRAILER, "an answer, not the trailer"
    assert answers_of(path, body)[0].state.connected is False


def test_a_subscriber_shows_up_in_the_observer_list_by_the_name_it_sent(edge, rig):
    """A panel names itself with a header, and the daemon reads it the same way
    it reads a client's metadata. **This is the call path that was broken.**

    Looked at while the stream is open, because hanging up is the whole of
    unsubscribing — a check afterwards would find an empty list whether or not
    anything was ever registered, which is the way this test would pass for the
    wrong reason.
    """
    path = "/statemachined.v1.State/WatchState"
    _status, _body, watching = post(
        edge,
        path,
        request(path),
        GRPC_WEB,
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
    path = "/statemachined.v1.State/WatchState"
    post(edge, path, request(path), GRPC_WEB, frames=1)
    status, trailer, answers = unary(edge, "/statemachined.v1.Configuration/ReadHealth")
    assert status == 200 and trailer["grpc-status"] == "0" and answers[0].ok is True
