# SPDX-License-Identifier: LGPL-3.0-or-later
"""The transport: one newline-delimited line in, one out.

USB CDC is what the reference board offers and what docs/operations/bringup.md is written
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
import time

import serial

from .device_line_monitor import FROM_DEVICE, TO_DEVICE

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
        on_line_observed=None,
    ):
        #: Called with ("to_device"|"from_device", line) for every whole line
        #: that crosses this link. Here rather than in the session above,
        #: because a monitor that only saw what the session understood would
        #: miss exactly what somebody opens a monitor for: the junk, the reply
        #: to a command that had already timed out, the line with the bad CRC.
        #:
        #: Wrapped in try/except at the call sites: a monitor that could break
        #: the link would be worse than no monitor.
        self.on_line_observed = on_line_observed
        self.url = to_url(target)
        self._timeout = timeout
        #: Whatever has arrived and is not yet a whole line. See read_line().
        self._receive_buffer = bytearray()
        # baudrate is meaningless for socket:// and harmless there; passing it
        # unconditionally keeps one construction path for both transports.
        self._port = serial.serial_for_url(self.url, baudrate=baud, timeout=timeout)

    def write_line(self, line: str) -> None:
        self._port.write((line + "\n").encode("ascii"))
        self._port.flush()
        self._observe(TO_DEVICE, line)

    def read_line(self, timeout: float | None = None) -> str | None:
        """One **complete** line, or None if none arrived before the timeout.

        `timeout` overrides the port's for this call. A daemon polling the link
        between commands wants tens of milliseconds where a command waiting for
        its reply wants seconds, and the difference matters: a poll that blocked
        for the command timeout would hold the device lock for that long and
        make every request wait behind it.

        A timeout is not an error here. Sitting quiet is what a healthy device
        does between replies, and `monitor` spends its whole life doing it.

        Buffered rather than `readline()`, and that is a correctness fix rather
        than a performance one: pyserial's `readline()` returns whatever it has
        when the timeout expires, newline or not, so a line that straddled a
        timeout would be delivered in halves and both halves would fail their
        CRC. It never bit at a two-second timeout and a board that writes a line
        in microseconds; it bites immediately once anything polls the link on a
        short timeout, which is what a daemon that must stay responsive between
        commands has to do.
        """
        effective_timeout = self._timeout if timeout is None else timeout
        # The port's own timeout as well, not only this loop's deadline. A
        # `read()` that is already waiting does not care what this function
        # decided afterwards, so a short poll behind a long port timeout would
        # block for the long one -- which is the whole failure this parameter
        # exists to prevent.
        if self._port.timeout != effective_timeout:
            self._port.timeout = effective_timeout
        deadline = time.monotonic() + effective_timeout
        while True:
            newline_at = self._receive_buffer.find(b"\n")
            if newline_at >= 0:
                line = self._receive_buffer[:newline_at]
                del self._receive_buffer[: newline_at + 1]
                text = line.decode("ascii", errors="replace").rstrip("\r")
                self._observe(FROM_DEVICE, text)
                return text
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            waiting = getattr(self._port, "in_waiting", 0) or 1
            chunk = self._port.read(waiting)
            if chunk:
                self._receive_buffer.extend(chunk)

    def _observe(self, direction: str, line: str) -> None:
        if self.on_line_observed is None:
            return
        try:
            self.on_line_observed(direction, line)
        except Exception:  # noqa: BLE001 -- see on_line_observed
            pass

    def reset_input(self) -> None:
        """Drop whatever is already buffered.

        A board that has been running on its own has been talking to nobody, and
        a serial monitor left open earlier may have left a partial line in the
        driver; starting a session on top of that produces one spurious framing
        complaint.
        """
        self._receive_buffer.clear()
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
