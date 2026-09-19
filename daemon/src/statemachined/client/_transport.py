# SPDX-License-Identifier: LGPL-3.0-or-later
"""One request, one refusal shape, and the URL arithmetic. Nothing else.

Every call in `client.py` goes through :class:`Transport`, which is the only
place in this package that knows what an HTTP status is. That is deliberate:
the rule that a refusal keeps the daemon's own error code (`errors.py`) is one
rule, and a package that applied it in thirty methods would apply it in
twenty-nine.

**The `httpx.Client` is injectable and that is load-bearing**, not a testing
convenience. `httpx.Client(transport=httpx.ASGITransport(app))` and Starlette's
`TestClient` both satisfy it, so a caller can point this at a daemon running in
its own process -- which is what makes the integration suite here, and triald's
end-to-end suite, exercise the real far end rather than a mock of it.
"""

from __future__ import annotations

from typing import Any

import httpx

from .errors import TransportError, refusal_from

#: How long to wait on a call. Generous, because the far end may be uploading a
#: graph set to a microcontroller over a serial link -- `POST
#: /api/session/graphs` is tens of seconds on a UART rig -- and finite, because
#: a dead rig should be a failed call rather than a hung session.
DEFAULT_TIMEOUT_SECONDS = 10.0

#: What `POST /api/session/graphs` and `POST /api/session/open` get instead.
#: They are the slowest calls in the API by a wide margin and they happen once,
#: before an animal is in the booth; timing them out at ten seconds would turn
#: the call that exists to let a session fail early into the thing that fails.
UPLOAD_TIMEOUT_SECONDS = 120.0


class Transport:
    """A base URL, an HTTP client, and the refusal rule."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        """Close the HTTP client, if this object opened it.

        A client handed in from outside is not closed: whoever opened it owns
        it, and a library that closed its caller's connection pool would be a
        library you can only use once.
        """
        if self._owns_client:
            self._http.close()

    # ------------------------------------------------------------- the verbs ---

    def get(self, path: str, *, doing: str, params: dict | None = None, **kwargs) -> Any:
        return self._request("GET", path, doing=doing, params=params, **kwargs)

    def post(self, path: str, body: Any = None, *, doing: str, **kwargs) -> Any:
        return self._request("POST", path, doing=doing, json=body, **kwargs)

    def put(self, path: str, body: Any = None, *, doing: str, **kwargs) -> Any:
        return self._request("PUT", path, doing=doing, json=body, **kwargs)

    def patch(self, path: str, body: Any = None, *, doing: str, **kwargs) -> Any:
        return self._request("PATCH", path, doing=doing, json=body, **kwargs)

    def delete(self, path: str, *, doing: str, **kwargs) -> Any:
        return self._request("DELETE", path, doing=doing, **kwargs)

    def _request(
        self,
        method: str,
        path: str,
        *,
        doing: str,
        timeout_seconds: float | None = None,
        **kwargs,
    ) -> Any:
        """Send it, and turn anything that is not a 2xx into a refusal.

        `doing` is a phrase, not a sentence -- "arming trial 193", "uploading
        the session's graph set" -- and it is on every call because the message
        a person reads at two in the morning should say what was being attempted
        as well as why it failed.
        """
        # On every request rather than on the client, so that `timeout_seconds`
        # means the same thing whoever opened the `httpx.Client`. A client handed
        # in from outside carries its own default -- httpx's is five seconds --
        # and a caller who asked this object for thirty would otherwise silently
        # get five.
        kwargs["timeout"] = (
            timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        )
        try:
            response = self._http.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise TransportError(f"the rig did not answer while {doing}: {exc}") from exc

        if response.status_code >= 400:
            raise refusal_from(response.status_code, _body_of(response), doing)
        if not response.content:
            # `DELETE` and a handful of others answer with nothing. An empty
            # dict rather than None, so a caller never has to know which.
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise TransportError(
                f"the rig answered {doing} with something that is not JSON: "
                f"{response.text[:200]!r}"
            ) from exc


def _body_of(response: httpx.Response) -> Any:
    """The refusal's own words, whatever shape they came in.

    A proxy in front of a rig answers HTML, and a client that raised while
    building the exception for that would hide the one clue there was.
    """
    try:
        return response.json()
    except ValueError:
        return response.text


def websocket_url_for(base_url: str, path: str, **query: str | None) -> str:
    """`ws(s)://` for a path under `base_url`, with the query it was given.

    Derived from the base URL's scheme rather than configured separately: a rig
    reached over `https` serves its streams over `wss` and there is no rig where
    those differ, so a second setting would only be a second thing to get wrong.
    """
    scheme = "wss" if base_url.startswith("https") else "ws"
    rest = base_url.rstrip("/").split("://", 1)[-1]
    given = "&".join(f"{key}={value}" for key, value in query.items() if value)
    return f"{scheme}://{rest}{path}" + (f"?{given}" if given else "")
