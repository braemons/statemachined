# SPDX-License-Identifier: LGPL-3.0-or-later
"""One refusal shape, and the rule that every refusal names what to change.

The wire's errors follow that rule -- `PROTOCOL.md` §5, where every `error`
carries a `context` naming the field -- and it is worth keeping one layer up.
An HTTP 409 that says "conflict" tells a person nothing; one that says the
device is holding set 7 and you asked for a graph that is not in it tells them
what to do next.
"""

from __future__ import annotations

from fastapi import HTTPException

from ..device.message_framing import DeviceRefusedTheCommand


def refusal(status_code: int, error: str, detail: str, context: str = "") -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": error, "detail": detail, "context": context or error},
    )


def no_device_connected(detail: str = "no device is connected") -> HTTPException:
    return refusal(503, "not_connected", detail, "device")


def from_device_refusal(exc: DeviceRefusedTheCommand) -> HTTPException:
    """A device's own refusal, passed up with its own code.

    Not flattened into a generic 409 message: the device said `graph_mismatch`
    or `busy` or `bad_index`, those are the words its documentation uses, and a
    caller switching on them should be switching on the device's answer rather
    than on the daemon's paraphrase of it.
    """
    return refusal(409, exc.code, exc.message or str(exc), exc.context)
