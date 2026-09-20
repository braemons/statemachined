# SPDX-License-Identifier: LGPL-3.0-or-later
"""The browser's way in: the panels, and the Connect protocol over them.

**A browser cannot speak gRPC.** It has no access to HTTP trailers and no
control over HTTP/2 framing, which is why gRPC-Web and Connect exist at all.
Rust solves this in-process with `tonic-web`; Python has no maintained
equivalent — `sonora` was last released in 2023 — and the one live option,
`connectrpc`, brings a second protobuf runtime with it. So this is written
here, against the protocol specification, and it is small because the Connect
protocol is small:

* **A unary call is an HTTP POST.** The body is the bare message; the answer is
  the bare message; an error is a JSON object with a status code. No framing,
  no trailers, no HTTP/2.
* **A server-streaming call** is the same POST, answering with enveloped
  frames — one flag byte, four length bytes, the message — and a final frame
  whose flag bit says "end of stream" and which carries the error if there was
  one. Still no trailers.

That is the whole protocol surface a browser needs, and it is why this file is
short enough to own.

**Nothing here knows what a graph is.** It dispatches by descriptor into the
same servicers `grpc_server.py` registers, so the two transports cannot drift:
an rpc added to the proto and implemented once is reachable from both. The
daemon's own port speaks real gRPC, for clients, CLIs and grpcurl; this is the
edge, and a rig with no browser on it loses nothing by never starting it.
"""

from __future__ import annotations

import base64
import json
import logging
import struct
from collections.abc import Callable
from pathlib import Path

import grpc
from google.protobuf import json_format
from google.protobuf.message import Message
from google.protobuf.message_factory import GetMessageClass

from statemachined._proto.statemachined.v1 import service_pb2
from statemachined.daemon.api.rig_service import RigService
from statemachined.daemon.api.servicers.refusals import REFUSAL_METADATA_KEY

log = logging.getLogger(__name__)

#: The Connect content types this edge accepts. Binary for the panels, JSON so
#: that a person debugging can read a request in the network tab.
_UNARY_CODECS = {"application/proto": "proto", "application/json": "json"}
_STREAM_CODECS = {"application/connect+proto": "proto", "application/connect+json": "json"}

#: `EndStreamResponse` is flagged, not framed differently: bit 1 of the flag
#: byte. Bit 0 would mean the message is compressed, which this never does.
_END_OF_STREAM = 0b10

#: gRPC's codes, as Connect spells them and as HTTP answers them. Connect uses
#: the same names gRPC does, which is the point of it.
_CONNECT_CODE = {
    grpc.StatusCode.INVALID_ARGUMENT: ("invalid_argument", 400),
    grpc.StatusCode.NOT_FOUND: ("not_found", 404),
    grpc.StatusCode.FAILED_PRECONDITION: ("failed_precondition", 412),
    grpc.StatusCode.UNIMPLEMENTED: ("unimplemented", 501),
    grpc.StatusCode.UNAVAILABLE: ("unavailable", 503),
    grpc.StatusCode.INTERNAL: ("internal", 500),
    grpc.StatusCode.UNKNOWN: ("unknown", 500),
}


class Aborted(Exception):
    """What a servicer's `context.abort()` raises on this transport.

    The servicers were written against `grpc.aio.ServicerContext` and touch
    exactly one method of it. That is what lets the same code serve two
    protocols: this transport supplies a context whose `abort` raises, and
    everything else about a servicer is unaware there are two.
    """

    def __init__(self, code: grpc.StatusCode, detail: str, metadata) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.metadata = dict(metadata or ())


class EdgeContext:
    """The servicer context, as much of it as a servicer actually uses."""

    async def abort(self, code, details="", trailing_metadata=()):
        raise Aborted(code, details, trailing_metadata)


class Rpc:
    """One method: how to decode its request, call it, and encode its answer."""

    def __init__(self, handler: Callable, request_type, streaming: bool) -> None:
        self.handler = handler
        self.request_type = request_type
        self.streaming = streaming


def rpcs_of(servicers: dict[str, object]) -> dict[str, Rpc]:
    """Every rpc in the proto, by the path Connect addresses it at.

    Built from the descriptor rather than written out: the path is
    `/statemachined.v1.Session/Open`, the request type comes from the descriptor, and
    an rpc added to `service.proto` becomes reachable here as soon as something
    implements it. There is no list to forget to update.
    """
    table: dict[str, Rpc] = {}
    for name, descriptor in service_pb2.DESCRIPTOR.services_by_name.items():
        servicer = servicers[name]
        for method in descriptor.methods:
            handler = getattr(servicer, method.name)
            table[f"/{descriptor.full_name}/{method.name}"] = Rpc(
                handler,
                GetMessageClass(method.input_type),
                streaming=method.server_streaming,
            )
    return table


# -- the protocol ---------------------------------------------------------------


def _decode(rpc: Rpc, codec: str, body: bytes) -> Message:
    message = rpc.request_type()
    if codec == "json":
        # Strict, as §11 requires of a request: an unknown field is refused by
        # name here, before a servicer sees it. The binary path cannot do that
        # in the parser, which is why the servicers ask separately.
        json_format.Parse(body or b"{}", message, ignore_unknown_fields=False)
    else:
        message.ParseFromString(body)
    return message


def _encode(codec: str, message: Message) -> bytes:
    if codec == "json":
        return json_format.MessageToJson(
            message,
            always_print_fields_with_no_presence=True,
            preserving_proto_field_name=False,
            use_integers_for_enums=False,
        ).encode()
    return message.SerializeToString()


def _envelope(flags: int, payload: bytes) -> bytes:
    """One frame of a Connect stream: a flag byte, a big-endian length, the body."""
    return struct.pack(">BI", flags, len(payload)) + payload


def _error_body(problem: Aborted) -> tuple[bytes, int]:
    """A Connect error, and the HTTP status that carries it.

    The typed refusal goes in `details` rather than being dropped: this is the
    same `statemachined.v1.Error` the gRPC transport puts in trailing metadata, so a
    browser and a Python client read the same three fields and neither has to
    parse a sentence.
    """
    code, status = _CONNECT_CODE.get(problem.code, ("unknown", 500))
    body: dict = {"code": code, "message": problem.detail}
    refusal = problem.metadata.get(REFUSAL_METADATA_KEY)
    if refusal is not None:
        # Padded: this is `google.protobuf.Any` in JSON, which is standard
        # base64. gRPC-Web's unpadded `-bin` metadata is a different rule on a
        # different transport, and the browser client here reads this one.
        body["details"] = [
            {"type": "statemachined.v1.Error", "value": base64.b64encode(refusal).decode()}
        ]
    return json.dumps(body).encode(), status


async def _send(send, status: int, content_type: str, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type.encode()),
                *(_CORS_HEADERS),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive) -> bytes:
    body = b""
    while True:
        event = await receive()
        body += event.get("body", b"")
        if not event.get("more_body", False):
            return body


async def _call_unary(rpc: Rpc, codec: str, body: bytes, send) -> None:
    try:
        answer = await rpc.handler(_decode(rpc, codec, body), EdgeContext())
    except Aborted as problem:
        payload, status = _error_body(problem)
        return await _send(send, status, "application/json", payload)
    except json_format.ParseError as problem:
        return await _send(
            send,
            400,
            "application/json",
            json.dumps({"code": "invalid_argument", "message": str(problem)}).encode(),
        )
    await _send(send, 200, f"application/{codec}", _encode(codec, answer))


async def _call_streaming(rpc: Rpc, codec: str, body: bytes, send, receive) -> None:
    """A server stream: 200 immediately, frames as they come, an end frame last.

    The status is sent before anything is known about how the call will go,
    which is the protocol's own rule and the reason the end frame carries the
    error: by the time a stream fails, the status line is long gone.
    """
    content_type = f"application/connect+{codec}"
    try:
        request = _decode(rpc, codec, body)
    except json_format.ParseError as problem:
        return await _send(
            send,
            400,
            "application/json",
            json.dumps({"code": "invalid_argument", "message": str(problem)}).encode(),
        )

    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", content_type.encode()), *_CORS_HEADERS],
        }
    )

    end: dict = {}
    try:
        async for frame in rpc.handler(request, EdgeContext()):
            await send(
                {
                    "type": "http.response.body",
                    "body": _envelope(0, _encode(codec, frame)),
                    "more_body": True,
                }
            )
    except Aborted as problem:
        code, _status = _CONNECT_CODE.get(problem.code, ("unknown", 500))
        end = {"error": {"code": code, "message": problem.detail}}
    except (ConnectionResetError, BrokenPipeError):
        # The browser navigated away mid-stream. Not a failure of anything,
        # and nothing left to send it.
        return
    await send(
        {
            "type": "http.response.body",
            "body": _envelope(_END_OF_STREAM, json.dumps(end).encode()),
            "more_body": False,
        }
    )


# -- the panels -----------------------------------------------------------------


def find_web_root() -> Path:
    """Where the panels are.

    One function, already written: `web_user_interface_routes.web_directory`
    answers this for the routes, and a second answer here would be a second
    place for the packaged-versus-authored fallback to be got wrong.
    """
    from .web_user_interface_routes import web_directory

    return web_directory()


#: Enough to serve what this UI is made of, and no more. An unknown suffix is
#: refused rather than served as a guess.
_CONTENT_TYPE_BY_SUFFIX = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}

#: **CORS is open, and `/elements/` is why.** A console served from somewhere
#: else imports these panels by URL and calls this daemon from its own origin;
#: that is the contract, not an accident.
_CORS_HEADERS = [
    (b"access-control-allow-origin", b"*"),
    (b"access-control-allow-headers", b"*"),
    (b"access-control-expose-headers", b"*"),
    (b"cache-control", b"no-cache, must-revalidate"),
]


def _read_asset(root: Path, relative: str) -> tuple[bytes, str] | None:
    """One file under `root`, or nothing.

    `resolve()` and then a containment check, because `..` in a URL is how a
    static file server becomes a way to read `/etc/shadow`.
    """
    path = (root / relative).resolve()
    if not path.is_file() or root.resolve() not in path.parents:
        return None
    content_type = _CONTENT_TYPE_BY_SUFFIX.get(path.suffix)
    if content_type is None:
        return None
    return path.read_bytes(), content_type


# -- the app --------------------------------------------------------------------


def build_edge(service: RigService, servicers: dict[str, object]):
    """An ASGI app: the panels, and the rpcs a browser can reach.

    `servicers` is the same mapping `grpc_server.py` registers, so both
    transports call one implementation. Nothing is wired by name here.
    """
    table = rpcs_of(servicers)
    web_root = find_web_root()
    if not web_root.is_dir():  # pragma: no cover - only if built without the UI
        log.warning("no web UI at %s; serving the rpcs only", web_root)

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"]
        method = scope["method"]

        if method == "OPTIONS":
            # The preflight a browser sends before a cross-origin POST.
            return await _send(send, 204, "text/plain", b"")

        if method == "POST":
            rpc = table.get(path)
            if rpc is None:
                return await _send(
                    send,
                    404,
                    "application/json",
                    json.dumps(
                        {"code": "unimplemented", "message": f"no rpc at {path}"}
                    ).encode(),
                )
            content_type = _header(scope, b"content-type").split(";")[0].strip()
            codecs = _STREAM_CODECS if rpc.streaming else _UNARY_CODECS
            codec = codecs.get(content_type)
            if codec is None:
                return await _send(
                    send,
                    415,
                    "application/json",
                    json.dumps(
                        {
                            "code": "invalid_argument",
                            "message": f"{content_type or 'no content type'} is not one of "
                            f"{sorted(codecs)}",
                        }
                    ).encode(),
                )
            body = await _read_body(receive)
            if rpc.streaming:
                # The request message of a stream is enveloped like a response
                # frame: strip the five-byte header before parsing it.
                body = body[5:] if len(body) >= 5 else b""
                return await _call_streaming(rpc, codec, body, send, receive)
            return await _call_unary(rpc, codec, body, send)

        if method == "GET":
            # `/ui/<file>` is the published address of this daemon's own shell
            # assets, and `/elements/<file>` is the `/elements/` contract a
            # console in another repository fetches by URL. The routes needed
            # the `/ui/` prefix to keep static files out of the way of `/api/`;
            # nothing here needs it, because an rpc is a POST and a file is a
            # GET. It is kept anyway and mapped, rather than dropped: the
            # address is somebody else's, and a page that 404s because a prefix
            # was tidied away is a page nobody can debug from the outside.
            relative = "index.html" if path == "/" else path.lstrip("/")
            if relative.startswith("ui/"):
                relative = relative[len("ui/") :]
            asset = _read_asset(web_root, relative)
            if asset is None:
                return await _send(send, 404, "text/plain", f"no {relative}".encode())
            content, content_type = asset
            return await _send(send, 200, content_type, content)

        await _send(send, 405, "text/plain", b"")

    return app


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode()
    return ""
