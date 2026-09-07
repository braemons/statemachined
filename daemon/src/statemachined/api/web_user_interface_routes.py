# SPDX-License-Identifier: LGPL-3.0-or-later
"""Serving the web UI, and the `/elements/` contract. dev/DAEMON.md §5.

No build step, no framework, no CDN -- triald's rule, for triald's reason: a rig
box may have no route to the internet and a browser in a booth must not wait on
unpkg. So the UI is files, shipped as package data, served from here.

**`/elements/` is a public contract and `/ui/` is not.** A console in another
repo loads `/elements/statemachined.js` and drops `<statemachined-device>` into
its own page; that URL and the element names are what this branch owes it. What
`/ui/` holds is this daemon's own shell -- the page you get by pointing a
browser at the rig -- and it may be rearranged freely.

**Nothing is cached.** One daemon serves both the elements and the API they
call, which is what keeps them the same version; a browser holding yesterday's
element against today's API would give that guarantee away for a few kilobytes.
"""

from __future__ import annotations

from importlib.resources import as_file, files
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

from .http_errors import refusal

router = APIRouter(tags=["web"])

#: Enough to serve what this UI is made of, and no more. An unknown suffix is
#: refused rather than served as a guess: a static server that will hand out any
#: file with any type is a larger promise than "here is a page".
CONTENT_TYPE_BY_SUFFIX = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}

NO_CACHE_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}


def web_directory() -> Path:
    """Where the UI's files are, whether that is a checkout or a wheel.

    `as_file` rather than a path built from `__file__`, so this keeps working if
    the package is ever installed zipped -- which the vendored-interpreter
    packaging of dev/DAEMON.md §6.1 does not do today and could tomorrow.
    """
    with as_file(files("statemachined") / "web") as path:
        return Path(path)


def read_asset(relative_path: str, root: Path) -> FileResponse:
    """One file under `root`, or a refusal that says which file.

    The containment check is not decoration. This is the only route in the
    daemon that turns a URL into a filesystem path, and a rig daemon runs as a
    system user with a graph store and a config file worth reading.
    """
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise refusal(404, "no_such_asset", f"{relative_path!r} is not in the UI", "path")
    if not candidate.is_file():
        raise refusal(404, "no_such_asset", f"the UI has no {relative_path!r}", "path")
    content_type = CONTENT_TYPE_BY_SUFFIX.get(candidate.suffix)
    if content_type is None:
        raise refusal(
            404,
            "unserved_file_type",
            f"{candidate.suffix!r} is not a type this daemon serves",
            "path",
        )
    return FileResponse(candidate, media_type=content_type, headers=NO_CACHE_HEADERS)


@router.get("/", include_in_schema=False)
def read_index() -> HTMLResponse:
    """The rig's own page. Every panel on it is one of the elements below."""
    index = web_directory() / "index.html"
    return HTMLResponse(index.read_text(), headers=NO_CACHE_HEADERS)


@router.get("/elements/{relative_path:path}", include_in_schema=False)
def read_element_module(relative_path: str) -> FileResponse:
    """The public contract: a console in another repo loads these by URL."""
    return read_asset(relative_path, web_directory() / "elements")


@router.get("/ui/{relative_path:path}", include_in_schema=False)
def read_shell_asset(relative_path: str) -> FileResponse:
    """This daemon's own shell. Not a contract; rearrange at will."""
    return read_asset(relative_path, web_directory())
