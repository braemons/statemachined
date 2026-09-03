# SPDX-License-Identifier: LGPL-3.0-or-later
"""The transport: one newline-delimited line in, one out.

USB CDC is what the reference board offers and what dev/BRINGUP.md is written
around, but nothing in the protocol is serial -- it is lines of ASCII with a
CRC, which is as true of a TCP socket to an ethernet-attached MCU as it is of a
tty. So the target is a URL, pyserial's `serial_for_url` resolves it, and the
layers above this file never learn which they got. A future transport that
pyserial does not speak (a raw datagram link, say) becomes another class here
with the same three methods, and nothing else in the tool moves.

The one thing that is *not* transport-independent is fail-safe: BRINGUP.md §6
turns on `hal::link_up()` going false when a USB port closes. A device on a
switch has to decide for itself what a dead peer looks like -- a missed `ping`,
most likely, which is what `ping` arms the watchdog for -- and that is a
firmware question, not one this file can answer.
"""

from __future__ import annotations

import re

import serial

DEFAULT_TARGET = "/dev/ttyACM0"
DEFAULT_BAUD = 115200
DEFAULT_TIMEOUT = 2.0

_HOST_PORT = re.compile(r"^(?P<host>[A-Za-z0-9_.\-]+|\[[0-9A-Fa-f:]+\]):(?P<port>\d+)$")


def to_url(target: str) -> str:
    """Turn what somebody typed into a pyserial URL.

    A device path stays a device path; `host:port` becomes `socket://host:port`
    so the ethernet case needs no scheme from a person's fingers; anything that
    already has a scheme (`socket://`, `rfc2217://`, `loop://`, ...) is passed
    through untouched.
    """
    if "://" in target:
        return target
    if _HOST_PORT.match(target):
        return "socket://" + target
    return target


class SerialLink:
    """A line channel to exactly one device."""

    def __init__(
        self,
        target: str = DEFAULT_TARGET,
        baud: int = DEFAULT_BAUD,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.url = to_url(target)
        # baudrate is meaningless for socket:// and harmless there; passing it
        # unconditionally keeps one construction path for both transports.
        self._port = serial.serial_for_url(self.url, baudrate=baud, timeout=timeout)

    def write_line(self, line: str) -> None:
        self._port.write((line + "\n").encode("ascii"))
        self._port.flush()

    def read_line(self) -> str | None:
        """One line, or None if the read timed out.

        A timeout is not an error here. Sitting quiet is what a healthy device
        does between replies, and `monitor` spends its whole life doing it.
        """
        raw = self._port.readline()
        if not raw:
            return None
        return raw.decode("ascii", errors="replace").rstrip("\r\n")

    def reset_input(self) -> None:
        """Drop whatever is already buffered.

        A board in demo mode has been talking to nobody, and a serial monitor
        left open earlier may have left a partial line in the driver; starting a
        session on top of that produces one spurious framing complaint.
        """
        try:
            self._port.reset_input_buffer()
        except (OSError, serial.SerialException, AttributeError):
            pass  # Not every URL handler implements it; nothing depends on it.

    def close(self) -> None:
        self._port.close()

    def __enter__(self) -> "SerialLink":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
