# SPDX-License-Identifier: LGPL-3.0-or-later
"""The FastAPI application, assembled from the routers.

Small on purpose. Everything with a decision in it is in a router or in
`RigService`; what is decided here is only what is true of the whole surface:
that the service is reachable from every request, that CORS is open, and that
the link thread starts with the app and stops with it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..mdns_service_advertisement import MdnsServiceAdvertisement
from ..rig_configuration import RigConfiguration
from . import (
    configuration_routes,
    device_routes,
    graph_routes,
    recording_routes,
    session_routes,
    state_and_trace_routes,
    state_machine_config_routes,
    trial_routes,
    web_user_interface_routes,
)
from .rig_service import RigService
from .servicers import build_servicers
from .web_edge import build_edge


def create_application(
    configuration: RigConfiguration,
    advertisement: MdnsServiceAdvertisement | None = None,
    service: RigService | None = None,
) -> FastAPI:
    """The whole surface: the API, the UI that uses only the API, and the record
    that says this rig exists.

    The mDNS advertisement is passed in rather than built here because only the
    caller knows the port the server was actually told to listen on -- and a
    record advertising a port nothing is listening on is worse than no record.
    """
    # Built here unless the caller has one. `statemachined serve` builds it
    # first so that the gRPC server and this app are two faces of **one**
    # rig — two `RigService` objects would be two daemons fighting over one
    # serial port, which is the failure this argument exists to prevent.
    service = service if service is not None else RigService(configuration)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.start()
        # After the service, and withdrawn before it stops: a console that finds
        # a rig should find one that can answer. Advertising is never fatal --
        # see mdns_service_advertisement.py -- so nothing here is guarded.
        if advertisement is not None:
            advertisement.start()
        try:
            yield
        finally:
            if advertisement is not None:
                advertisement.stop()
            service.stop()

    application = FastAPI(
        title="statemachined",
        summary="Owns one statemachined device: the wire, the graphs, and the API over both",
        version="0.0.0",
        lifespan=lifespan,
    )
    application.state.rig_service = service

    # The web UI's elements are meant to be embedded in a console served from
    # somewhere else (docs/developer/daemon.md §5), so nothing may assume same-origin. The
    # price of that decision is paid here, in one line.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(device_routes.router)
    application.include_router(graph_routes.router)
    application.include_router(trial_routes.router)
    application.include_router(state_and_trace_routes.router)
    application.include_router(state_and_trace_routes.trace_router)
    application.include_router(configuration_routes.router)
    # The two halves of the configuration, next to each other in the surface as
    # they are on disk: the box, and what the box is doing today.
    application.include_router(state_machine_config_routes.router)
    application.include_router(session_routes.router)
    # The trace is always on; this is what keeps a named piece of it. Beside the
    # session routes because that is the pairing in practice: a session opens, a
    # recording starts, and on a rig with no triald those are the two acts.
    application.include_router(recording_routes.router)
    # Last, because its `/ui/{path}` and `/elements/{path}` are the only
    # catch-all routes in the app and a router that matched before them would be
    # shadowed. Nothing here is a private route: the UI uses only what is above.
    application.include_router(web_user_interface_routes.router)

    @application.get("/api/health", tags=["device"])
    def read_health() -> dict:
        """Is the daemon up, and does it have a device.

        Two different questions and systemd only asks the first: a daemon whose
        board is unplugged is still the thing you ask *why*.
        """
        return {"ok": True, "device_connected": service.supervisor.is_connected}

    # **The rpcs, on the same port, while both surfaces exist.**
    #
    # The panels are a generated Connect client now and address
    # `/statemachined.v1.<Service>/<Rpc>`. `statemachined.client` — the base
    # tier of this package, which triald depends on — and the e2e suite still
    # call `/api/...`. Both have to work on one port until that client is
    # converted too, so the edge answers the rpc addresses and the app keeps
    # everything else.
    #
    # A middleware rather than a mount, because an rpc address is at the root
    # and a mount needs a prefix. It goes when the routes do, and then
    # `statemachined serve` runs the edge by itself.
    application.add_middleware(
        RpcAddressesGoToTheEdge, edge=build_edge(service, build_servicers(service))
    )
    return application


class RpcAddressesGoToTheEdge:
    """Hand `/statemachined.v1.*` to the Connect edge, everything else onward.

    Pure ASGI rather than a Starlette `BaseHTTPMiddleware`: the edge streams,
    and `BaseHTTPMiddleware` buffers a response before passing it on, which
    would turn `WatchTrace` into a call that never returns.
    """

    PREFIX = "/statemachined.v1."

    def __init__(self, app, edge) -> None:
        self.app = app
        self.edge = edge

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith(self.PREFIX):
            return await self.edge(scope, receive, send)
        return await self.app(scope, receive, send)
