# SPDX-License-Identifier: LGPL-3.0-or-later
"""What each line is called, and what the rig did to it.

Two different things live here and the difference is load-bearing.

**Names are the daemon's alone and never reach the wire.** `lever_left` is line
4 to the device and nothing else; renaming it is free, changes no graph, and
needs no upload. That is what lets a graph be authored against words rather than
against a pinout somebody has to remember.

**The wiring does reach the wire**, as dev/PROTOCOL.md 3.5's `wiring` command:
invert, enable, debounce and the output safe levels. It describes the box rather
than the paradigm -- see firmware/core/io/wiring.h for why that distinction was
worth a firmware change -- so it lives on the line map, is sent once when a rig
is wired, and is not part of any graph.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: One `uint32_t` of input word on the reference board. Widening it is a type
#: change reaching every struct in the firmware and the wire format, so a line
#: map that asks for more is refused here rather than truncated there.
MAXIMUM_LINE_COUNT = 32


class InputLineDefinition(BaseModel):
    """One input line: what it is called, and how the rig conditions it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    line_index: int = Field(ge=0, lt=MAXIMUM_LINE_COUNT)

    #: Reads active-low. Opto-isolated inputs routinely do, and this is applied
    #: when the word is assembled so that no predicate above it has a special
    #: case (firmware/core/io/input_conditioner.h).
    reads_active_low: bool = False

    #: Whether the line participates at all. A disabled line reads zero however
    #: the pin is driven, which is how a rig says "nothing is plugged in there"
    #: without every graph having to avoid naming it.
    is_enabled: bool = True

    #: How long a change must hold before it is believed. Per line rather than
    #: per predicate, because a predicate over several lines is what chatters
    #: worst: two lines each bouncing once can make a combination rise and fall
    #: several times, and no care in the graph can filter that afterwards.
    debounce_milliseconds: int = Field(default=0, ge=0, le=65535)

    #: Free text for whoever is holding the board -- "D6", "screw terminal 3".
    #: Never sent anywhere; it exists so the web UI can say where to put a wire.
    pin_label: str = ""


class OutputLineDefinition(BaseModel):
    """One output line: what it is called, and what "off" means for it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    line_index: int = Field(ge=0, lt=MAXIMUM_LINE_COUNT)

    #: The level this line is driven to on watchdog timeout, reset, link loss or
    #: a refused graph. Per line, because "off" is not always "low": an
    #: active-low valve driver is *opened* by a low, and a board that came up
    #: driving every line low would open it on every power cycle.
    safe_level_is_high: bool = False

    pin_label: str = ""


class LineMap(BaseModel):
    """Every line this rig has, by name.

    The one object that knows both halves: which word a name means, and what the
    device must be told about the wiring behind it.
    """

    model_config = ConfigDict(extra="forbid")

    input_lines: list[InputLineDefinition] = Field(default_factory=list)
    output_lines: list[OutputLineDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _refuse_duplicate_names_and_indices(self) -> LineMap:
        for kind, definitions in (("input", self.input_lines), ("output", self.output_lines)):
            seen_names: set[str] = set()
            seen_indices: set[int] = set()
            for definition in definitions:
                if definition.name in seen_names:
                    raise ValueError(f"two {kind} lines are called {definition.name!r}")
                # Two names for one line is not a harmless alias: a graph naming
                # both would raise one line and believe it had raised two, and
                # the mistake is invisible in the record.
                if definition.line_index in seen_indices:
                    raise ValueError(
                        f"two {kind} lines are line {definition.line_index}: "
                        f"{definition.name!r} is a second name for it"
                    )
                seen_names.add(definition.name)
                seen_indices.add(definition.line_index)
        return self

    # ------------------------------------------------------------ lookup ---

    def input_line_index_for_name(self, line_name: str) -> int:
        """The bit position `line_name` means, or a ValueError naming what exists."""
        for definition in self.input_lines:
            if definition.name == line_name:
                return definition.line_index
        known = ", ".join(sorted(definition.name for definition in self.input_lines))
        raise ValueError(f"no input line is called {line_name!r}. This rig has: {known or '(none)'}")

    def output_line_index_for_name(self, line_name: str) -> int:
        """The bit position `line_name` means, or a ValueError naming what exists."""
        for definition in self.output_lines:
            if definition.name == line_name:
                return definition.line_index
        known = ", ".join(sorted(definition.name for definition in self.output_lines))
        raise ValueError(
            f"no output line is called {line_name!r}. This rig has: {known or '(none)'}"
        )

    def input_line_mask_for_names(self, line_names: list[str]) -> int:
        """The mask a predicate over these names becomes.

        Order does not matter and repetition is harmless, which is why this
        returns a mask rather than a list: `all` over a set of lines is a single
        `and` on the device, evaluated in the scan.
        """
        mask = 0
        for line_name in line_names:
            mask |= 1 << self.input_line_index_for_name(line_name)
        return mask

    # -------------------------------------------------------------- wire ---

    def wiring_message_fields(self) -> dict[str, object]:
        """The body of dev/PROTOCOL.md 3.5's `wiring`, as this rig needs it.

        Every field, always, rather than only what differs from the default: the
        command replaces what the board holds, and a partial one would leave a
        board that had been moved between rigs carrying half of each.
        """
        invert_mask = 0
        enable_mask = 0
        debounce_milliseconds_per_line = [0] * MAXIMUM_LINE_COUNT
        for definition in self.input_lines:
            if definition.reads_active_low:
                invert_mask |= 1 << definition.line_index
            if definition.is_enabled:
                enable_mask |= 1 << definition.line_index
            debounce_milliseconds_per_line[definition.line_index] = (
                definition.debounce_milliseconds
            )

        safe_level_mask = 0
        for output_definition in self.output_lines:
            if output_definition.safe_level_is_high:
                safe_level_mask |= 1 << output_definition.line_index

        # Trailing zeros are dropped because the device fills the rest with
        # zeros anyway, and a 32-entry array of nothing is most of a protocol
        # line's budget (dev/PROTOCOL.md 3.5).
        while debounce_milliseconds_per_line and debounce_milliseconds_per_line[-1] == 0:
            debounce_milliseconds_per_line.pop()

        return {
            "invert": invert_mask,
            "enable": enable_mask,
            "safe": safe_level_mask,
            "debounce_ms": debounce_milliseconds_per_line,
        }
