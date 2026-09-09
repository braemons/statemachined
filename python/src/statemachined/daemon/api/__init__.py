# SPDX-License-Identifier: LGPL-3.0-or-later
"""The HTTP surface, as dev/API.md specifies it.

One router per subject, and a `RigService` underneath them that owns the device.
Routers hold no state: everything that outlives a request lives in the service,
because two requests arriving at once must not each think they own the link.
"""

try:
    import fastapi as _fastapi  # noqa: F401  imported for the check below
except ModuleNotFoundError as missing:  # pragma: no cover - an install-shape error
    # `No module named 'fastapi'` is true and useless. Every other refusal in
    # this project names what to change, and an import is where somebody meets
    # the tiering for the first time. Here rather than in `statemachined.daemon`
    # because that package also holds the stores and the recorder, which need
    # pydantic and nothing else -- reading a saved config should not require a
    # web framework.
    if missing.name != "fastapi":
        raise
    raise ModuleNotFoundError(
        "statemachined.daemon.api needs fastapi and uvicorn, which are the "
        "`serve` extra and are not installed.\n"
        "  pip install 'statemachined[serve]'   (or: uv add 'statemachined[serve]')\n"
        "This package is tiered on purpose: the base is the documents and the "
        "HTTP client, so something that only talks to a daemon never installs a "
        "web framework in order to do it."
    ) from missing
