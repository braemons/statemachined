# SPDX-License-Identifier: LGPL-3.0-or-later
"""The daemon's own port: gRPC, for clients, CLIs and grpcurl.

The browser gets `web_edge.py` beside this, because a browser cannot speak
gRPC — it has no access to HTTP trailers and no control over HTTP/2 framing.
**Both dispatch into the same servicers**, so the two transports cannot drift:
an rpc implemented once is reachable from both, and there is no private path
for either.

`REGISTRARS` is read off the generated module by name rather than written out,
so a service added to the proto is registered as soon as something implements
it — and `tests/unit/test_every_rpc_is_implemented.py` is what says something
does.
"""

from __future__ import annotations

import grpc

from statemachined._proto.statemachined.v1 import service_pb2, service_pb2_grpc
from statemachined.daemon.api.rig_service import RigService
from statemachined.daemon.api.servicers import build_servicers

#: How many concurrent rpcs the server will run. The servicers push their
#: blocking work onto threads of their own (`refusals.answering`), so this
#: bounds the *loop's* concurrency rather than the device's — the device is
#: serialised by `RigService.device_lock` whatever this says.
MAXIMUM_CONCURRENT_RPCS = 64


def registrars() -> dict:
    """`{service name: add_XServicer_to_server}`, from the descriptor."""
    return {
        name: getattr(service_pb2_grpc, f"add_{name}Servicer_to_server")
        for name in service_pb2.DESCRIPTOR.services_by_name
    }


def grpc_port_for(web_port: int) -> int:
    """Where gRPC listens, given the port the panels are served on.

    **Two listeners, one number to configure.** `port` stays what it has always
    been — where the panels and the `/elements/` contract live, which is what a
    console's `rigs.json` holds and what a person types into a browser — and
    gRPC goes one above it. A Python daemon cannot serve both on one socket the
    way a Rust one can: `grpc.aio` owns its port outright, and no ASGI server
    speaks native gRPC. This is where that difference surfaces, and it surfaces
    as a `+ 1` rather than as a second setting nobody remembers to change.

    triald does exactly this, and its client's `DEFAULT_PORT` is the same rule
    written on the other side.
    """
    return web_port + 1


def build_server(service: RigService, address: str) -> tuple[grpc.aio.Server, dict]:
    """A gRPC server with every service registered, not yet started.

    Returns the servicers as well, so that the browser edge can be built from
    the same objects rather than from a second set that would drift.
    """
    servicers = build_servicers(service)
    server = grpc.aio.server(maximum_concurrent_rpcs=MAXIMUM_CONCURRENT_RPCS)
    for name, register in registrars().items():
        register(servicers[name], server)
    server.add_insecure_port(address)
    return server, servicers
