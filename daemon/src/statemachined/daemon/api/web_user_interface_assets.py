# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where the web UI's files are, and which of them this daemon will serve.

No build step for the *panels*, no framework, no CDN -- triald's rule, for
triald's reason: a rig box may have no route to the internet and a browser in a
booth must not wait on unpkg. So the UI is files, shipped as package data, and
the one generated artifact among them (`elements/daemon_api_client.js`) is
committed rather than built here.

**`/elements/` is a public contract and `/ui/` is not.** A console in another
repository loads `/elements/statemachined.js` and drops `<statemachined-device>`
into its own page; that URL and the element names are what this branch owes it.
What `/ui/` holds is this daemon's own shell -- the page you get by pointing a
browser at the rig -- and it may be rearranged freely.

**Nothing is cached.** One daemon serves both the elements and the API they
call, which is what keeps them the same version; a browser holding yesterday's
element against today's API would give that guarantee away for a few kilobytes.

This module holds no routes and imports no web framework. It was
`web_user_interface_routes.py` and had three FastAPI handlers on it; the app is
gone and `web_edge.py` is the only thing that serves a file now, so what is
left is the two facts a server needs — where the files are, and what a `.js` is
— in one place rather than two.
"""

from __future__ import annotations

from importlib.resources import as_file, files
from pathlib import Path

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


def web_directory() -> Path:
    """Where the UI's files are, installed or in a checkout.

    The panels are authored in `client/web/`, a sibling of `daemon/` rather
    than a subdirectory of it, so that somebody looking for this daemon's UI
    finds it without knowing how the Python package is laid out
    (`contracts/DAEMON_LAYOUT.md`). A wheel cannot ship a directory from
    outside its own root, so `packaging/Makefile` copies it to
    `statemachined/daemon/web` before building.

    An *editable* install applies no such copy, which is the case a developer
    is always in: there, the only copy is the authored one. Trying the packaged
    location first means a real install never touches the filesystem outside
    itself.

    `as_file` rather than a path built from `__file__`, so this keeps working
    if the package is ever installed zipped -- which the vendored-interpreter
    packaging of docs/developer/daemon.md §6.1 does not do today and could
    tomorrow.
    """
    with as_file(files("statemachined.daemon") / "web") as path:
        packaged = Path(path)
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[5] / "client" / "web"


def read_asset(root: Path, relative_path: str) -> tuple[bytes, str] | None:
    """One file under `root` and its content type, or nothing.

    **The containment check is not decoration.** This is the only place in the
    daemon that turns a URL into a filesystem path, and a rig daemon runs as a
    system user with a graph store and a config file worth reading. `resolve()`
    first and then check, because `..` in a URL is how a static file server
    becomes a way to read `/etc/shadow`.

    `None` for all three ways of not being a file to serve -- outside the root,
    not there, or a suffix this daemon does not serve -- because the caller
    answers 404 to each of them and telling them apart would tell somebody
    probing which of their guesses was closest.
    """
    path = (root / relative_path).resolve()
    if not path.is_file() or root.resolve() not in path.parents:
        return None
    content_type = CONTENT_TYPE_BY_SUFFIX.get(path.suffix)
    if content_type is None:
        return None
    return path.read_bytes(), content_type
