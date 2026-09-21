# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browser's way in: the panels, and gRPC-Web over them.

**A browser cannot speak gRPC.** It has no access to HTTP trailers and no
control over HTTP/2 framing, which is why gRPC-Web exists at all. Rust solves
this in-process with `tonic-web`; Python has no maintained equivalent —
`sonora` was last released in 2023 — so this is written here, against the
protocol specification, and it is short because gRPC-Web is a small change to
gRPC rather than a protocol of its own:

* **A call is an HTTP POST.** Request and answer are *framed*: one flag byte,
  four big-endian length bytes, the binary message. Unary and streaming alike;
  a stream is simply more answer frames.
* **The trailers move into the body.** The last frame has bit 7 of its flag
  byte set and carries `grpc-status`, `grpc-message` and any `-bin` metadata as
  ASCII `key: value` lines. That is the whole reason a browser can speak this
  and cannot speak gRPC.
* **The HTTP status is 200 even when the call was refused.** The outcome lives
  in the trailer, because by the time a stream fails the status line is long
  gone — and answering refusals two different ways depending on whether the rpc
  streams would be two code paths where the protocol has one.

**Binary only**, one codec: `application/grpc-web+proto`. This edge used to
speak the Connect protocol and carry a JSON codec beside the binary one, so
that a person debugging could read a request in the network tab. That is gone
on purpose — one codec, one code path — and `grpcurl` against the daemon's own
port is what replaced it, which is better because it works for the Python
clients too.

**Nothing here knows what a graph is.** It dispatches by descriptor into the
same servicers `grpc_server.py` registers, so the two transports cannot drift:
an rpc added to the proto and implemented once is reachable from both. The
daemon's own port speaks real gRPC, for clients, CLIs and grpcurl; this is the
edge, and a rig with no browser on it loses nothing by never starting it.
"""

from __future__ import annotations

import base64
import logging
import struct
from collections.abc import Callable
from urllib.parse import quote

import grpc
from google.protobuf.message import Message
from google.protobuf.message_factory import GetMessageClass

from statemachined._proto.statemachined.v1 import service_pb2
from statemachined.daemon.api.rig_service import RigService
from statemachined.daemon.api.servicers.refusals import REFUSAL_METADATA_KEY
from statemachined.daemon.api.web_user_interface_assets import read_asset, web_directory

log = logging.getLogger(__name__)

#: The one content type this edge speaks. `application/grpc-web` is the same
#: thing spelled shorter — the proto codec is the default — so both are taken
#: and neither is a second codec.
_CONTENT_TYPE = "application/grpc-web+proto"
_ACCEPTED = {_CONTENT_TYPE, "application/grpc-web"}

#: Bit 7 of the flag byte marks the trailer frame. Bit 0 would mean the message
#: is compressed, which this never does.
_TRAILER = 0x80


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
    """The servicer context, as much of it as a servicer actually uses.

    **"As much as a servicer uses" is a promise this class has to keep**, and
    it broke once: `observer_registration.watching` began calling
    `invocation_metadata()` and `peer()`, this had neither, and every stream a
    browser opened died with an `AttributeError` — while the gRPC path, which
    every integration test uses, was perfectly fine. `tests/unit/` drives a
    stream through the edge now so the two surfaces cannot drift again.

    The three methods are the whole of it: `abort` raises, and the other two
    answer out of the ASGI scope in the shape grpcio answers them.
    """

    def __init__(self, scope: dict | None = None) -> None:
        self._scope = scope or {}

    async def abort(self, code, details="", trailing_metadata=()):
        raise Aborted(code, details, trailing_metadata)

    def invocation_metadata(self):
        """The request's headers, as gRPC spells metadata: lowercase pairs.

        A browser cannot set arbitrary gRPC metadata, but it can set a header,
        and `observer-name` is one — which is how a panel names itself in the
        observer list exactly as a client does.
        """
        return tuple(
            (key.decode().lower(), value.decode("utf-8", "replace"))
            for key, value in self._scope.get("headers", ())
        )

    def peer(self) -> str:
        """Who is calling, in grpcio's own `ipv4:host:port` spelling.

        Not decoration: the observer list is read to answer "is something
        connected and receiving nothing", and an entry with no address cannot
        be told from another tab's.
        """
        client = self._scope.get("client")
        if not client:
            return "unknown"
        host, port = client
        kind = "ipv6" if ":" in str(host) else "ipv4"
        return f"{kind}:{host}:{port}"


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


def _frame(flags: int, payload: bytes) -> bytes:
    """One frame: a flag byte, a big-endian length, the body."""
    return struct.pack(">BI", flags, len(payload)) + payload


def _unframe(body: bytes) -> bytes:
    """The one message in a request body, out of its frame.

    A request carries exactly one — every rpc here is unary or server-streaming,
    so nothing this edge serves takes a stream of requests. A body that is
    shorter than its own header is an empty message rather than an error: that
    is what a request with no fields looks like when a client sends nothing.
    """
    if len(body) < 5:
        return b""
    (length,) = struct.unpack(">I", body[1:5])
    return body[5 : 5 + length]


def _trailer(problem: Aborted | None) -> bytes:
    """The last frame: how the call went, as ASCII `key: value` lines.

    **This is the whole reason a browser can speak gRPC-Web.** gRPC puts these
    in HTTP trailers, which a browser cannot read; here they are the final
    frame of the body, which it can.

    The typed refusal rides along as itself rather than being dropped: the same
    `statemachined.v1.Error` the gRPC transport puts in trailing metadata, so a
    browser and a Python client read the same three fields and neither has to
    parse a sentence.
    """
    if problem is None:
        return _frame(_TRAILER, b"grpc-status: 0\r\n")

    code = problem.code.value[0] if hasattr(problem.code, "value") else int(problem.code)
    # Percent-encoded, because a refusal's sentence is a person's words and a
    # header line cannot carry every byte of them.
    lines = [f"grpc-status: {code}", f"grpc-message: {quote(problem.detail or '', safe='')}"]
    refusal = problem.metadata.get(REFUSAL_METADATA_KEY)
    if refusal is not None:
        # Unpadded, which is gRPC's rule for a `-bin` value and *not* the
        # padded base64 the Connect protocol used in its JSON `details`. The
        # browser client strips and re-adds the padding itself.
        lines.append(
            f"{REFUSAL_METADATA_KEY}: {base64.b64encode(refusal).decode().rstrip('=')}"
        )
    return _frame(_TRAILER, ("\r\n".join(lines) + "\r\n").encode())


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


async def _start(send) -> None:
    """A 200 and the headers, before anything is known about how the call goes.

    Every gRPC-Web answer begins this way, refusals included — the outcome is
    in the trailer. It is the protocol's own rule for streams, and following it
    for unary calls too is what keeps this one code path instead of two.
    """
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", _CONTENT_TYPE.encode()), *_CORS_HEADERS],
        }
    )


async def _read_body(receive) -> bytes:
    body = b""
    while True:
        event = await receive()
        body += event.get("body", b"")
        if not event.get("more_body", False):
            return body


async def _call_unary(rpc: Rpc, body: bytes, send, scope: dict) -> None:
    request = rpc.request_type()
    request.ParseFromString(_unframe(body))
    await _start(send)
    try:
        answer = await rpc.handler(request, EdgeContext(scope))
    except Aborted as problem:
        return await send({"type": "http.response.body", "body": _trailer(problem)})
    await send(
        {
            "type": "http.response.body",
            "body": _frame(0, answer.SerializeToString()),
            "more_body": True,
        }
    )
    await send({"type": "http.response.body", "body": _trailer(None), "more_body": False})


async def _call_streaming(rpc: Rpc, body: bytes, send, receive, scope: dict) -> None:
    """A server stream: 200 immediately, frames as they come, the trailer last.

    Identical in shape to the unary case above, because in this protocol it is
    the same thing with more answer frames.
    """
    request = rpc.request_type()
    request.ParseFromString(_unframe(body))
    await _start(send)

    problem: Aborted | None = None
    try:
        async for frame in rpc.handler(request, EdgeContext(scope)):
            await send(
                {
                    "type": "http.response.body",
                    "body": _frame(0, frame.SerializeToString()),
                    "more_body": True,
                }
            )
    except Aborted as failure:
        problem = failure
    except (ConnectionResetError, BrokenPipeError):
        # The browser navigated away mid-stream. Not a failure of anything,
        # and nothing left to send it.
        return
    await send({"type": "http.response.body", "body": _trailer(problem), "more_body": False})


# -- the panels -----------------------------------------------------------------

#: **CORS is open, and `/elements/` is why.** A console served from somewhere
#: else imports these panels by URL and calls this daemon from its own origin;
#: that is the contract, not an accident.
_CORS_HEADERS = [
    (b"access-control-allow-origin", b"*"),
    (b"access-control-allow-headers", b"*"),
    # Named rather than left to `*`: a cross-origin reader is not required to
    # honour the wildcard for every header, and these three are how a refusal
    # reaches a panel at all. They are sent in the trailer frame here, not as
    # headers, but a client that reads either way should find them.
    (
        b"access-control-expose-headers",
        b"*, grpc-status, grpc-message, " + REFUSAL_METADATA_KEY.encode(),
    ),
    (b"cache-control", b"no-cache, must-revalidate"),
]


# -- the app --------------------------------------------------------------------


def build_edge(service: RigService, servicers: dict[str, object]):
    """An ASGI app: the panels, and the rpcs a browser can reach.

    `servicers` is the same mapping `grpc_server.py` registers, so both
    transports call one implementation. Nothing is wired by name here.
    """
    table = rpcs_of(servicers)
    web_root = web_directory()
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
                # `unimplemented`, in the trailer, exactly as a refusal from a
                # servicer would be: a client that has one way to read an
                # outcome should not need a second for this one.
                await _start(send)
                return await send(
                    {
                        "type": "http.response.body",
                        "body": _trailer(
                            Aborted(grpc.StatusCode.UNIMPLEMENTED, f"no rpc at {path}", ())
                        ),
                    }
                )
            content_type = _header(scope, b"content-type").split(";")[0].strip()
            if content_type not in _ACCEPTED:
                # 415 rather than a trailer: this one is about HTTP, not about
                # the call, and a caller sending the wrong content type has not
                # made a gRPC request to answer.
                return await _send(
                    send,
                    415,
                    "text/plain",
                    f"{content_type or 'no content type'} is not {_CONTENT_TYPE}".encode(),
                )
            body = await _read_body(receive)
            if rpc.streaming:
                return await _call_streaming(rpc, body, send, receive, scope)
            return await _call_unary(rpc, body, send, scope)

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
            asset = read_asset(web_root, relative)
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
