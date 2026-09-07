# SPDX-License-Identifier: LGPL-3.0-or-later
"""Line number to pin label, for printing only.

The map itself is a wire contract and it lives in dev/HARDWARE.md; this table
is a convenience so that `io.in` prints as something a person can hold against
the wires in front of them. It is keyed by the `board` string in `hello_ack`,
and a board that is not in it -- the next MCU, on the far end of an ethernet
cable -- prints bare line numbers rather than a plausible lie about somebody
else's pinout.
"""

from __future__ import annotations

PIN_MAPS: dict[str, dict[str, list[str]]] = {
    # dev/HARDWARE.md, "Uno R4 Minima -- line map". D0/D1 are the UART and D13
    # is the on-board LED, so neither is a line.
    "uno_r4_minima": {
        "in": ["D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9"],
        "out": ["D10", "D11", "D12", "A0", "A1", "A2", "A3", "A4"],
    },
}


def pin_label(board: str | None, direction: str, line: int) -> str:
    """`"3 (A0)"` where the pinout is known, `"3"` where it is not."""
    pins = PIN_MAPS.get(board or "", {}).get(direction, [])
    if line < len(pins):
        return f"{line} ({pins[line]})"
    return str(line)


def word_bits(word: int, count: int) -> str:
    """A line mask as a row of characters, line 0 on the left.

    Left to right in line order, which is the opposite of how a binary literal
    reads and the same as how the table in HARDWARE.md does. Anybody comparing
    this against a breadboard is counting from line 0.
    """
    return "".join("1" if word & (1 << i) else "." for i in range(count))


def high_lines(board: str | None, direction: str, word: int, count: int) -> str:
    lines = [pin_label(board, direction, i) for i in range(count) if word & (1 << i)]
    return ", ".join(lines) if lines else "none"
