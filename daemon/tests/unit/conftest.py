# SPDX-License-Identifier: LGPL-3.0-or-later
"""A rig that is nothing but a recording of what was asked of it.

`httpx.MockTransport` is the whole of it: every request lands in one handler,
which writes it down and answers whatever the test told it to. No daemon, no
device, no socket -- because what these tests are about is the half of this
client that is *not* the daemon: which path each method calls, what it puts in
the body, which timeout it chooses, and what it makes of an answer.

That half is worth testing on its own precisely because the layers above cannot
see it fail cleanly: a client that posted to `/api/trial/start` with the wrong
field name would fail in the integration suite as a `422`, and the message would
be about the daemon's validator rather than about the line that is wrong.
"""

from __future__ import annotations

import json

import httpx
import pytest
from statemachined.client import StatemachinedClient


class RecordedCall:
    """One request, in the terms a test asserts on."""

    def __init__(self, request: httpx.Request) -> None:
        self.method = request.method
        self.url = request.url
        self.path = request.url.path
        self.query = dict(request.url.params)
        self.headers = request.headers
        self.extensions = request.extensions
        raw = request.content
        self.body = json.loads(raw) if raw else None

    @property
    def timeout_seconds(self):
        """What httpx was told to wait, which is what a timeout choice means here."""
        return self.extensions.get("timeout", {}).get("read")

    def __repr__(self) -> str:
        return f"<{self.method} {self.path} {self.body!r}>"


class FakeRig:
    """Answers a queue of prepared replies, and remembers every call."""

    def __init__(self) -> None:
        self.calls: list[RecordedCall] = []
        self._replies: list[httpx.Response] = []
        self.default_reply: httpx.Response | None = None

    # ------------------------------------------------------------- arrange ---

    def will_answer(self, body=None, status_code: int = 200) -> FakeRig:
        self._replies.append(httpx.Response(status_code, json=body if body is not None else {}))
        return self

    def will_refuse(self, status_code: int, error: str, detail: str, context: str = "") -> FakeRig:
        """The daemon's own refusal shape: every refusal names what to change."""
        return self.will_answer(
            {"detail": {"error": error, "detail": detail, "context": context}}, status_code
        )

    def will_answer_with_text(self, text: str, status_code: int = 200) -> FakeRig:
        self._replies.append(httpx.Response(status_code, text=text))
        return self

    # ---------------------------------------------------------------- serve ---

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(RecordedCall(request))
        if self._replies:
            return self._replies.pop(0)
        if self.default_reply is not None:
            return self.default_reply
        return httpx.Response(200, json={})

    # ---------------------------------------------------------------- assert ---

    @property
    def call(self) -> RecordedCall:
        """The only call, when a test made one. Fails loudly when it made more.

        A test asserting on `calls[0]` while the method under test quietly made
        three would pass, and the extra two are exactly the kind of thing this
        suite exists to notice.
        """
        assert len(self.calls) == 1, f"expected one call, got {self.calls}"
        return self.calls[0]


@pytest.fixture
def rig() -> FakeRig:
    return FakeRig()


@pytest.fixture
def client(rig: FakeRig) -> StatemachinedClient:
    """The client under test, wired to the fake rig over a mock transport.

    `http://rig.test:8081` rather than localhost, so that a test asserting on a
    URL is asserting on the client's arithmetic rather than on a default that
    happens to match.
    """
    return StatemachinedClient(
        "http://rig.test:8081", http_client=httpx.Client(transport=httpx.MockTransport(rig))
    )
