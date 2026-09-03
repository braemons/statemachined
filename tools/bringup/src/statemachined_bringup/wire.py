# SPDX-License-Identifier: GPL-3.0-or-later
"""Framing, borrowed from the emulator tests rather than written again.

There are deliberately two implementations of dev/PROTOCOL.md in this
repository -- the firmware's and the one under emulation/tests/, written from
the document so that a test asks the device an independent question. A third
one here would not add a third opinion; it would only add somewhere for the
rules to drift. So this module imports the test's copy, and the price is that
the CLI has to be run from a checkout, which is the only place a bring-up tool
is ever run from anyway.

Replies, unlike commands, go through a real JSON parser. The test module avoids
one on purpose -- its assertions are about bytes on a wire -- but this tool's
job is to print a report to somebody holding a board, and json.loads is the
right tool for that once the CRC has already said the line arrived intact.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_RELATIVE = Path("emulation") / "tests"


def _load_protocol():
    try:
        import statemachined_protocol  # noqa: F401  (already importable)

        return statemachined_protocol
    except ImportError:
        pass
    # Editable-installed from tools/bringup/src/..., so the checkout is above
    # us; the CWD is tried too, for a copy installed some other way.
    roots = [Path(__file__).resolve(), Path.cwd().resolve() / "_"]
    for start in roots:
        for parent in start.parents:
            if (parent / _RELATIVE / "statemachined_protocol.py").is_file():
                sys.path.insert(0, str(parent / _RELATIVE))
                import statemachined_protocol

                return statemachined_protocol
    raise SystemExit(
        "error: cannot find emulation/tests/statemachined_protocol.py.\n"
        "       The bring-up CLI shares the emulator tests' framing rather than\n"
        "       carrying a second copy of them, so run it from a statemachined\n"
        "       checkout (or put that directory on PYTHONPATH)."
    )


_protocol = _load_protocol()

crc16_ccitt = _protocol.crc16_ccitt
statemachined_line = _protocol.statemachined_line


class WireError(Exception):
    """A line came back that is not a message: bad CRC, bad JSON, non-ASCII."""


def command_line(msg_type: str, seq: int, **fields) -> str:
    """A framed command line, ready for the wire, without its newline.

    Members are written in call order with `t` and `seq` first, and the CRC
    goes on last because the protocol requires it to be last -- that is what
    lets the device find it by scanning backwards instead of parsing first.
    """
    body = f'{{"t":"{msg_type}","seq":{seq}'
    for key, value in fields.items():
        if value is None:
            continue
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
        raise WireError("line contains a byte >= 0x80, which is a framing error")
    marker = ',"crc":"'
    at = line.rfind(marker)
    if at < 0 or not line.endswith('"}'):
        raise WireError(f"no trailing crc member: {line!r}")
    claimed = line[at + len(marker) : -2]
    actual = f"{crc16_ccitt(line[:at].encode('ascii')):04X}"
    if claimed.upper() != actual:
        raise WireError(f"crc mismatch: line says {claimed}, bytes say {actual}")
    try:
        return json.loads(line)
    except ValueError as exc:
        raise WireError(f"crc was good but the line is not JSON: {exc}") from exc


class DeviceError(Exception):
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
