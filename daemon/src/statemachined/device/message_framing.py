# SPDX-License-Identifier: LGPL-3.0-or-later
"""Framing: the CRC, the line, and reading one back.

This is the *third* implementation of dev/PROTOCOL.md's framing in this
repository, after the firmware's and the one under emulation/tests/. That is
deliberate and it is a cost. The two that already existed are independent on
purpose -- a test written from the document asks the device a question it did
not already know the answer to -- and until the daemon existed, this module
imported the test's copy rather than adding a third opinion.

An installed package cannot do that: a .deb has no emulation/ directory, and a
daemon that only runs from a checkout is not a daemon. So the rules are written
out here, and what keeps the three honest is tests/unit/test_message_framing.py: a fixed
set of lines with known CRCs, asserted against this module *and* against
emulation/tests/statemachined_protocol.py in the same run. A drift between the
two now fails a host-only test in `make ci` rather than a hardware suite
somebody runs on a Tuesday.

Replies, unlike commands, go through a real JSON parser. The emulator tests
avoid one on purpose -- their assertions are about bytes on a wire -- but this
module's callers act on what came back, and json.loads is the right tool for
that once the CRC has already said the line arrived intact.
"""

from __future__ import annotations

import json

from .message_vocabulary import Field

#: Both rolling checksums in this protocol -- the graph upload's and the
#: result's -- start here and fold the CRC-covered bytes of each line in order.
#: See dev/PROTOCOL.md §3.2 and §4.3.
CRC_INIT = 0xFFFF

_CRC_MEMBER = ',"crc":'


def crc16_ccitt(data: bytes, seed: int = CRC_INIT) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xor-out.

    `seed` is what makes this usable twice: once per line, and once folded
    across the covered bytes of many lines, which is the rolling checksum
    `graph_end` and `result_end` carry.
    """
    crc = seed
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def statemachined_line(body: str) -> str:
    """Close an object and append its CRC.

    `body` is the message without its closing brace, e.g.
    '{"msg_type":"ping","message_id":1'. The CRC covers everything before the
    `,"crc":` that carries it, which is what lets the device find and check it
    by arithmetic from the end of the line before parsing a byte.
    """
    return '%s,"crc":"%04X"}' % (body, crc16_ccitt(body.encode("ascii")))


def covered_bytes(line: str) -> bytes:
    """The part of a framed line a rolling checksum folds: everything before `crc`.

    The same span the device's own CRC covers, which is what makes the two
    checksums comparable at all.
    """
    return line[: line.rindex(_CRC_MEMBER)].encode("ascii")


def rolling_checksum(lines: list[str], seed: int = CRC_INIT) -> int:
    """One CRC accumulated across the covered bytes of several lines, in order.

    A message lost or reordered inside an upload is caught by this even though
    every line that did arrive passed its own check.
    """
    crc = seed
    for line in lines:
        crc = crc16_ccitt(covered_bytes(line), crc)
    return crc


class FramingError(Exception):
    """A line came back that is not a message: bad CRC, bad JSON, non-ASCII."""


def command_line(msg_type: str, message_id: int, **fields) -> str:
    """A framed command line, ready for the wire, without its newline.

    Members are written in call order with `msg_type` and `message_id` first,
    and the CRC goes on last because the protocol requires it to be last --
    that is what lets the device find it by scanning backwards instead of
    parsing first.
    """
    body = f'{{"{Field.MSG_TYPE}":"{msg_type}","{Field.MESSAGE_ID}":{message_id}'
    for key, value in fields.items():
        # None becomes `null`, and is not dropped: this protocol distinguishes
        # the two. `terminal` must be present on every graph_state, as an
        # outcome code or as null for a state that is not terminal, and a
        # device that silently accepted the member's absence would be guessing
        # which a graph meant. Omit a field by not passing it.
        body += f',"{key}":{json.dumps(value, separators=(",", ":"))}'
    return statemachined_line(body)


def parse_reply(line: str) -> dict:
    """Check a received line's CRC, then parse it.

    In that order, and never the other way round: a message whose CRC does not
    match is not acted on, and printing half of one to a person on a bench is
    acting on it.
    """
    line = line.strip()
    if not line.isascii():
        raise FramingError("line contains a byte >= 0x80, which is a framing error")
    marker = ',"crc":"'
    at = line.rfind(marker)
    if at < 0 or not line.endswith('"}'):
        raise FramingError(f"no trailing crc member: {line!r}")
    claimed = line[at + len(marker) : -2]
    actual = f"{crc16_ccitt(line[:at].encode('ascii')):04X}"
    if claimed.upper() != actual:
        raise FramingError(f"crc mismatch: line says {claimed}, bytes say {actual}")
    try:
        return json.loads(line)
    except ValueError as exc:
        raise FramingError(f"crc was good but the line is not JSON: {exc}") from exc


class DeviceRefusedTheCommand(Exception):
    """The device refused a command, which is a normal outcome and not a bug.

    Every refusal names what to change (PROTOCOL.md §5), so the `context` is
    the useful half and is never dropped.
    """

    def __init__(self, reply: dict):
        self.code = reply.get("code", "?")
        self.message = reply.get("message", "")
        self.context = reply.get("context", "")
        detail = ": ".join(x for x in (self.message, self.context) if x)
        super().__init__(f"{self.code} ({detail})" if detail else str(self.code))
