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

from ..daemon_configuration import DaemonConfiguration
from . import configuration_routes, device_routes, graph_routes, state_and_trace_routes
from . import trial_routes
from .rig_service import RigService


def create_application(configuration: DaemonConfiguration) -> FastAPI:
    service = RigService(configuration)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.start()
        try:
            yield
        finally:
            service.stop()

    application = FastAPI(
        title="statemachined",
        summary="Owns one statemachined device: the wire, the graphs, and the API over both",
        version="0.0.0",
        lifespan=lifespan,
    )
    application.state.rig_service = service

    # The web UI's elements are meant to be embedded in a console served from
    # somewhere else (dev/DAEMON.md §5), so nothing may assume same-origin. The
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

    @application.get("/api/health", tags=["device"])
    def read_health() -> dict:
        """Is the daemon up, and does it have a device.

        Two different questions and systemd only asks the first: a daemon whose
        board is unplugged is still the thing you ask *why*.
        """
        return {"ok": True, "device_connected": service.supervisor.is_connected}

    return application
