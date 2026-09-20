# SPDX-License-Identifier: LGPL-3.0-or-later
"""The API, as `proto/statemachined/v1/` describes it.

One servicer per service, a seam in `convert/` that is the only thing speaking
protobuf, and a `RigService` underneath them all that owns the device.
Servicers hold no state: everything that outlives a call lives in the service,
because two calls arriving at once must not each think they own the link.

**Two listeners, one set of servicers.** `grpc_server.py` is the daemon's own
port, for clients, CLIs and grpcurl; `web_edge.py` is beside it for a browser,
which cannot speak gRPC because it has no access to HTTP trailers and no
control over HTTP/2 framing. Both dispatch into the same objects, so the two
transports cannot drift.

**There is no `application.py` and there are no routes.** There were: a FastAPI
app and ten routers, which were a second description of this interface written
in URLs. The interface is authored in `proto/` now, so a web framework's schema
generation had nothing left to describe but itself.
"""

try:
    import grpc as _grpc  # noqa: F401  imported for the check below
except ModuleNotFoundError as missing:  # pragma: no cover - an install-shape error
    # `No module named 'grpc'` is true and useless. Every other refusal in this
    # project names what to change, and an import is where somebody meets the
    # tiering for the first time. Here rather than in `statemachined.daemon`
    # because that package also holds the stores and the recorder, which need
    # pydantic and nothing else -- reading a saved config should not require a
    # server.
    if missing.name != "grpc":
        raise
    raise ModuleNotFoundError(
        "statemachined.daemon.api needs grpcio and uvicorn, which are the "
        "`serve` extra and are not installed.\n"
        "  pip install 'statemachined[serve]'   (or: uv add 'statemachined[serve]')\n"
        "This package is tiered on purpose: the base is the documents, so "
        "something that only reads a saved graph never installs a server in "
        "order to do it.\n"
        "To talk to a daemon rather than be one, install `statemachined-client` "
        "instead -- it is a distribution of its own and needs neither."
    ) from missing
