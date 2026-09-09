# SPDX-License-Identifier: LGPL-3.0-or-later
"""What a refusal is, and why it is one class rather than a status code.

The daemon's rule is that **every refusal names what to change**: its errors are
`{"error": "<code>", "detail": "<sentence>", "context": "<field>"}` with a
matching HTTP status, and where the *device* refused, the code is the device's
own -- `busy`, `bad_index`, `graph_mismatch` -- rather than the daemon's
paraphrase of it (`api/http_errors.py`).

A client that flattened all of that into `HTTPStatusError: 409` would throw the
useful half away. So the shape survives the trip: :class:`Refused` carries the
code, the sentence and the field, and a caller switching on `error` is switching
on the same word the daemon's documentation uses.

The subclasses exist for the three cases a caller genuinely branches on -- it is
not here, you cannot do that now, there is no board -- and nothing else. A
status code that needs its own class is a status code with its own recovery.
"""

from __future__ import annotations

from typing import Any


class StatemachinedError(Exception):
    """Anything this client could not do. The one class to catch."""


class TransportError(StatemachinedError):
    """The rig did not answer at all: no route, no listener, a timed-out read.

    Distinct from :class:`Refused` because the recovery is different in kind. A
    refusal is an answer and tells you what to change; this is a silence, and
    the only thing it tells you is that whatever you were about to do has not
    happened -- which for `configure` means the trial is not armed.
    """


class Refused(StatemachinedError):
    """The rig answered, and said no.

    Attributes:
        status_code: what HTTP said.
        error: the daemon's own code, or the device's where the device refused.
        detail: a sentence saying what happened.
        context: the field to change. The daemon guarantees one.
        body: the whole response body, for the rare caller that wants more.
    """

    def __init__(
        self,
        status_code: int,
        error: str,
        detail: str,
        context: str = "",
        body: Any = None,
        doing: str = "",
    ) -> None:
        self.status_code = status_code
        self.error = error
        self.detail = detail
        self.context = context
        self.body = body
        self.doing = doing
        super().__init__(str(self))

    def __str__(self) -> str:
        where = f" while {self.doing}" if self.doing else ""
        change = f" (change: {self.context})" if self.context else ""
        return f"the rig refused{where}: {self.error}: {self.detail}{change}"


class NotFound(Refused):
    """404. The graph, the config, the recording or that trial is not there."""


class Conflict(Refused):
    """409. The rig's state does not admit the call, or the device refused.

    `no_graph_set`, `graph_not_in_set`, `busy`, `does_not_fit`, `graph_in_use`
    -- every one of them a thing that would be fine at another moment or with
    another argument, which is why they are not `NotFound` and not `Invalid`.
    """


class Invalid(Refused):
    """422. The request did not validate, or the board will not take it.

    Both FastAPI's own field errors and the daemon's
    `line_map_does_not_match_the_board`, which is refused *before anything is
    kept* -- so a rig that answers this is still running on what it had.
    """


class NotConnected(Refused):
    """503. There is no device.

    The daemon is up and answering -- that is why this is a refusal and not a
    :class:`TransportError` -- and it has no board. `GET /api/health` reports
    the two separately for exactly this reason: a daemon whose board is
    unplugged is still the thing you ask *why*.
    """


class TraceStreamLost(StatemachinedError):
    """The subscription fell out of the trace ring, and entries are gone.

    **Recoverable, and it must not pass silently.** `GET /api/trace/trial/{id}`
    answers exactly whatever the stream did, so nothing is unrecoverable; but a
    consumer that believed it saw everything is worse than one that knows it did
    not, which is why the daemon closes the socket rather than resuming from
    whatever is left.
    """

    def __init__(self, lost_from_entry_number: int, lost_to_entry_number: int) -> None:
        self.lost_from_entry_number = lost_from_entry_number
        self.lost_to_entry_number = lost_to_entry_number
        super().__init__(
            f"the trace subscription lost entries {lost_from_entry_number} "
            f"to {lost_to_entry_number}; fetch them with trace.for_trial(...)"
        )


#: The daemon's status codes, mapped to the classes above. Anything else -- a
#: 500, a proxy's 502 -- is a plain `Refused`, which is the honest answer: it is
#: an answer, and it is not one of the four this API documents.
_BY_STATUS: dict[int, type[Refused]] = {
    404: NotFound,
    409: Conflict,
    422: Invalid,
    503: NotConnected,
}


def refusal_from(status_code: int, body: Any, doing: str = "") -> Refused:
    """Build the exception for one refused response.

    Three shapes arrive here and all three are handled, because a client that
    raised `KeyError` on an unexpected error body would fail at exactly the
    moment somebody needed to read the message:

    * the daemon's own, `{"detail": {"error", "detail", "context"}}`;
    * FastAPI's validation shape, whose `detail` is a list naming the field;
    * anything else at all -- a proxy's HTML, an empty body -- which becomes
      the text it was, under the code `http_<status>`.
    """
    detail = body.get("detail") if isinstance(body, dict) else None
    cls = _BY_STATUS.get(status_code, Refused)

    if isinstance(detail, dict):
        return cls(
            status_code,
            str(detail.get("error", f"http_{status_code}")),
            str(detail.get("detail", detail)),
            str(detail.get("context", "")),
            body,
            doing,
        )
    if isinstance(detail, list):
        # FastAPI names the field in `loc`, and that is the whole value of this
        # shape: "body -> cap_milliseconds: Input should be >= 0" is actionable
        # and "422 Unprocessable Content" is not.
        return cls(
            status_code,
            "invalid_request",
            "; ".join(_one_validation_error(item) for item in detail),
            _first_field(detail),
            body,
            doing,
        )
    return cls(
        status_code,
        f"http_{status_code}",
        str(detail if detail is not None else body),
        "",
        body,
        doing,
    )


def _one_validation_error(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    where = " -> ".join(str(part) for part in item.get("loc", ()))
    return f"{where}: {item.get('msg', item)}" if where else str(item.get("msg", item))


def _first_field(detail: list) -> str:
    """The first field FastAPI named, so `context` means the same thing here."""
    for item in detail:
        if isinstance(item, dict) and item.get("loc"):
            return str(item["loc"][-1])
    return ""
