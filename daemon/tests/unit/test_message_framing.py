# SPDX-License-Identifier: GPL-3.0-or-later
"""The framing, against fixed bytes -- and against the other implementation.

`statemachined.device.message_framing` is the third implementation of dev/PROTOCOL.md's
framing in this repository. It exists because an installed package cannot
import `emulation/tests/statemachined_protocol.py`, and its risk is that the
two drift. This is the test that makes a drift fail here, in `make ci`, on a
machine with no board attached -- rather than on a bench, months later, as a
CRC error nobody can reproduce.

Two assertions, and the second is the one that matters:

1. Both implementations reproduce `wire_vectors.json`, whose CRCs were checked
   against the firmware's own crc16.cpp. That is what makes them *golden*: they
   are the device's arithmetic, not one host module's.
2. Both agree on bytes the vector file does not cover, generated here.

The emulator's copy is imported by path from the checkout and skipped when it
is not there, because a .deb has no emulation/ directory and this file travels
with the source tree, not with the package.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from statemachined.device.message_framing import (
    CRC_INIT,
    FramingError,
    command_line,
    covered_bytes,
    crc16_ccitt,
    parse_reply,
    rolling_checksum,
    statemachined_line,
)

VECTORS = json.loads((Path(__file__).parent / "wire_vectors.json").read_text())
LINES = VECTORS["lines"]
IDS = [entry["why"] for entry in LINES]


def _emulator_copy():
    """emulation/tests/statemachined_protocol.py, or None outside a checkout."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "emulation" / "tests"
        if (candidate / "statemachined_protocol.py").is_file():
            sys.path.insert(0, str(candidate))
            import statemachined_protocol

            return statemachined_protocol
    return None


emulator = _emulator_copy()
needs_checkout = pytest.mark.skipif(
    emulator is None, reason="emulation/tests is not present; run from a checkout"
)


# --------------------------------------------------------------- vectors ---


@pytest.mark.parametrize("entry", LINES, ids=IDS)
def test_daemon_reproduces_the_vector(entry):
    assert statemachined_line(entry["body"]) == entry["line"]


@needs_checkout
@pytest.mark.parametrize("entry", LINES, ids=IDS)
def test_emulator_reproduces_the_vector(entry):
    assert emulator.statemachined_line(entry["body"]) == entry["line"]


def test_rolling_checksum_matches_the_vector():
    over = [LINES[i]["line"] for i in VECTORS["rolling"]["over"]]
    assert f"{rolling_checksum(over):04X}" == VECTORS["rolling"]["checksum"]


@needs_checkout
def test_the_two_rolling_checksums_agree():
    over = [LINES[i]["line"] for i in VECTORS["rolling"]["over"]]
    bodies = [line[: line.rindex(',"crc":')] for line in over]
    assert f"{rolling_checksum(over):04X}" == emulator.graph_checksum(bodies)


# ------------------------------------------------- the two, on new bytes ---

# Shapes the vector file does not carry, so that agreement is not only
# agreement about five memorised lines.
GENERATED = [
    '{"msg_type":"cancel","message_id":65535,"trial_id":4294967295,"reason":"host"',
    '{"msg_type":"graph_dist","message_id":2,"i":0,"kind":"choice","opts":[100,200,300]',
    '{"msg_type":"configure","message_id":41,"trial_id":193,"set_version":7,"graph_index":2',
    '{"msg_type":"x","message_id":1,"s":"a space, a comma, and a \\"quote\\""',
]


@needs_checkout
@pytest.mark.parametrize("body", GENERATED)
def test_the_two_implementations_agree(body):
    assert statemachined_line(body) == emulator.statemachined_line(body)


@needs_checkout
def test_the_two_crcs_agree_on_the_empty_string():
    # The seed, unmodified. Worth pinning: an implementation that folded a
    # length or a terminator would differ here and nowhere else obvious.
    assert crc16_ccitt(b"") == emulator.crc16_ccitt(b"") == CRC_INIT


# ------------------------------------------------------------- the rules ---


def test_command_line_writes_null_rather_than_dropping_it():
    # PROTOCOL.md distinguishes absent from null: `terminal` must be present on
    # every graph_state, and a device that accepted its absence would be
    # guessing which a graph meant.
    line = command_line("graph_state", 6, i=1, terminal=None)
    assert '"terminal":null' in line


def test_command_line_puts_the_crc_last():
    # The device finds the CRC by scanning back from the end of the line,
    # before it parses a byte. A CRC anywhere else is not findable that way.
    line = command_line("ping", 1)
    assert line.endswith('"}') and ',"crc":"' in line
    assert line.index(',"crc":"') == line.rindex(',"crc":"')


def test_covered_bytes_stops_at_the_crc_member():
    entry = LINES[0]
    assert covered_bytes(entry["line"]) == entry["body"].encode("ascii")


def test_parse_reply_accepts_a_good_line():
    assert parse_reply(LINES[0]["line"])["msg_type"] == "ping"


def test_parse_reply_refuses_a_bad_crc():
    good = LINES[0]["line"]
    bad = good[: good.rindex('"crc":"') + 7] + "0000" + '"}'
    with pytest.raises(FramingError, match="crc mismatch"):
        parse_reply(bad)


def test_parse_reply_refuses_a_non_ascii_line():
    # A byte >= 0x80 is a framing error, not a character set question: the
    # protocol is ASCII and the CRC is over ASCII bytes.
    with pytest.raises(FramingError, match="0x80"):
        parse_reply('{"msg_type":"log","message_id":1,"m":"café","crc":"0000"}')


def test_parse_reply_refuses_a_line_with_no_crc_member():
    with pytest.raises(FramingError, match="no trailing crc"):
        parse_reply('{"msg_type":"ping","message_id":1}')
