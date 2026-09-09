# SPDX-License-Identifier: LGPL-3.0-or-later
"""What the board says its pins are called, and where that answer came from.

dev/PROTOCOL.md §3.6. Which pin a line is, and which direction it has, are fixed
when the firmware is compiled -- the HAL's tables are what `pinMode()` is called
over, and no command changes either. So a host cannot derive this; it can only
ask, or assume.

**Both are represented here, and they are not the same thing.** A map whose
`source` is `device` was read off the board this daemon is talking to. A map
whose `source` is `assumed` came from `board_pin_labels.py` -- a table in the
host, keyed by the board name, which is a hand-copied pin map and is therefore
exactly what this command exists to replace. Firmware flashed before §3.6
existed answers `no_pin_map`, which is not a failure and must not be treated as
one: a rig in a rack is reflashed when somebody gets to it.

What the difference is *for* is honesty in the API and the UI. A pin label the
board vouched for can be shown as fact; one the daemon assumed has to be shown
as an assumption, because the failure it hides -- a valve driven from a lever's
line number -- is silent everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: Where a pin label came from. `unknown` is a board with no entry in either
#: place: the next MCU, on the far end of an ethernet cable.
PinLabelSource = Literal["device", "assumed", "unknown"]


@dataclass(frozen=True)
class DevicePinMap:
    """One board's two line numberings, by label.

    Two lists, not one dictionary: input line 3 and output line 3 are different
    pins over different numberings, and a single mapping from label to index
    would collapse them -- which is a lever's number driving a valve.
    """

    input_pin_labels: list[str] = field(default_factory=list)
    output_pin_labels: list[str] = field(default_factory=list)
    source: PinLabelSource = "unknown"

    @property
    def came_from_the_device(self) -> bool:
        return self.source == "device"

    def label_for(self, direction: str, line_index: int) -> str:
        """The label of one line, or `""` where this map does not reach it."""
        labels = self.input_pin_labels if direction == "in" else self.output_pin_labels
        return labels[line_index] if 0 <= line_index < len(labels) else ""

    def line_index_for_label(self, direction: str, pin_label: str) -> int | None:
        """Which line that pin is, or None if this board has no such pin.

        Case-insensitive, because `a0` and `A0` are the same hole in the board
        and refusing a config over the difference would be pedantry with a
        soldering iron in the room.
        """
        labels = self.input_pin_labels if direction == "in" else self.output_pin_labels
        wanted = pin_label.strip().casefold()
        for line_index, label in enumerate(labels):
            if label.casefold() == wanted:
                return line_index
        return None

    def known_pins(self, direction: str) -> str:
        labels = self.input_pin_labels if direction == "in" else self.output_pin_labels
        return ", ".join(labels) or "(none)"
