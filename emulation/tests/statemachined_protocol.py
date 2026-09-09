# SPDX-License-Identifier: GPL-3.0-or-later
"""Speaking statemachined's wire protocol from Robot Framework.

Deliberately a *second* implementation of the framing rules rather than a
binding to the firmware's. If both ends were the same code, a test could only
ever prove the device agreed with itself; the whole value of an emulator test is
that something independent asks the device a question. This file is written from
docs/reference/protocol.md, not from firmware/core/protocol/.
"""


def crc16_ccitt(data, seed=0xFFFF):
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xor-out."""
    crc = seed
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def statemachined_line(body):
    """Close an object and append its CRC.

    `body` is the message without its closing brace, e.g. '{"msg_type":"ping","message_id":1'.
    The CRC covers everything before the `,"crc":` that carries it, which is what
    lets the device find and check it by arithmetic from the end of the line
    before parsing a byte.
    """
    covered = body.encode("ascii")
    return '%s,"crc":"%04X"}' % (body, crc16_ccitt(covered))


def graph_checksum(lines):
    """The rolling checksum over an upload, as `graph_end` must carry it.

    One CRC accumulated across the CRC-covered bytes of every graph message in
    order, so a message lost or reordered between graph_begin and graph_end is
    caught even though each line passed its own check.
    """
    crc = 0xFFFF
    for body in lines:
        crc = crc16_ccitt(body.encode("ascii"), crc)
    return "%04X" % crc


def field_of(line, key):
    """The raw value of a top-level key, as text. No JSON parser on purpose:
    a parser would accept things the bridge's would not, and these assertions
    are about bytes on a wire."""
    needle = '"%s":' % key
    at = line.find(needle)
    if at < 0:
        return None
    at += len(needle)
    end = at
    depth = 0
    in_str = False
    while end < len(line):
        c = line[end]
        if in_str:
            if c == '"' and line[end - 1] != "\\":
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            break
        end += 1
    return line[at:end].strip('"')


def line_codes(body):
    """The framed line plus its newline, as a list of byte values.

    Renode's `Write Line To Uart` does not reach this firmware -- the write side
    of the terminal tester silently delivers nothing here, though the read side
    works -- so lines go in a byte at a time through `sysbus.sci2 WriteChar`,
    which does. See emulation/README.md.
    """
    return [ord(c) for c in statemachined_line(body)] + [10]
