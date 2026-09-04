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

    #: Which bit of the input word this is -- the only part of a line that
    #: reaches the device. Optional, because `pin_label` can say the same thing
    #: in the terms a person can check against the board: see
    #: `LineMap.resolved_against`, which fills this in from what the device
    #: answered and refuses a pair that disagree.
    line_index: int | None = Field(default=None, ge=0, lt=MAXIMUM_LINE_COUNT)

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

    #: The pin, as the board names it: "D6", "TB1-3". Never sent anywhere -- the
    #: device knows its own pins -- but no longer free text either, because the
    #: device can now be asked what it calls them (dev/PROTOCOL.md 3.6). Where
    #: the board answered, this is *checked* against it, and it may be given
    #: instead of `line_index` rather than beside it.
    pin_label: str = ""


class OutputLineDefinition(BaseModel):
    """One output line: what it is called, and what "off" means for it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)

    #: See InputLineDefinition.line_index. Output line 3 is not input line 3:
    #: two numberings, two disjoint sets of pins.
    line_index: int | None = Field(default=None, ge=0, lt=MAXIMUM_LINE_COUNT)

    #: The level this line is driven to on watchdog timeout, reset, link loss or
    #: a refused graph. Per line, because "off" is not always "low": an
    #: active-low valve driver is *opened* by a low, and a board that came up
    #: driving every line low would open it on every power cycle.
    safe_level_is_high: bool = False

    #: See InputLineDefinition.pin_label.
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
                if definition.line_index is None and not definition.pin_label:
                    raise ValueError(
                        f"the {kind} line {definition.name!r} says neither which line it is "
                        f"nor which pin: give it a line_index, or a pin_label the board knows"
                    )
                # Two names for one line is not a harmless alias: a graph naming
                # both would raise one line and believe it had raised two, and
                # the mistake is invisible in the record.
                if definition.line_index is not None and definition.line_index in seen_indices:
                    raise ValueError(
                        f"two {kind} lines are line {definition.line_index}: "
                        f"{definition.name!r} is a second name for it"
                    )
                seen_names.add(definition.name)
                if definition.line_index is not None:
                    seen_indices.add(definition.line_index)
        return self

    # ------------------------------------------------------------ lookup ---

    def input_line_index_for_name(self, line_name: str) -> int:
        """The bit position `line_name` means, or a ValueError naming what exists."""
        for definition in self.input_lines:
            if definition.name == line_name:
                return _numbered(definition, "input")
        known = ", ".join(sorted(definition.name for definition in self.input_lines))
        raise ValueError(f"no input line is called {line_name!r}. This rig has: {known or '(none)'}")

    def output_line_index_for_name(self, line_name: str) -> int:
        """The bit position `line_name` means, or a ValueError naming what exists."""
        for definition in self.output_lines:
            if definition.name == line_name:
                return _numbered(definition, "output")
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

    # ------------------------------------------------- against a real board ---

    def resolved_against(self, pin_map) -> LineMap:
        """This map with every `line_index` filled in and checked against the board.

        dev/PROTOCOL.md §3.6 is what makes this possible: the board answers with
        the same table `pinMode()` was called over, so a pin label is no longer
        a comment. Three things happen here, and they are the whole point of the
        command existing:

          * a line that names only a **pin** gets its index from the board, so
            the config says the thing a person can check against the hardware
            in front of them rather than a bit position nothing is labelled
            with;
          * a line that names **both** has them checked, and a disagreement is
            refused. That is the silent wrong-valve bug: `pin_label = "A0"` on
            line 4 looks right in every listing and drives A1;
          * a line naming a **pin this board does not have** is refused, which
            is the typo that used to survive as far as an animal in the booth.

        **Only where the board actually answered.** A `pin_map` this daemon
        assumed -- `board_pin_labels.py`, for firmware older than §3.6 -- is
        advisory: it fills in a missing index, and it never overrules or refuses
        one that was written down. Refusing on a hand-copied table would be
        asserting the very thing this exists to stop asserting.

        Raises ValueError naming the fix. The caller closes the link on one:
        masks built from a wrong index are not something to push and then warn
        about.
        """
        return LineMap(
            input_lines=[
                definition.model_copy(
                    update={"line_index": _resolve_one(definition, "in", pin_map)}
                )
                for definition in self.input_lines
            ],
            output_lines=[
                definition.model_copy(
                    update={"line_index": _resolve_one(definition, "out", pin_map)}
                )
                for definition in self.output_lines
            ],
        )

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
            # Resolved, never as configured: a map whose indices came from pin
            # labels is only numbered once `resolved_against` has asked the
            # board, and masks are the last place to discover that.
            line_index = _numbered(definition, "input")
            if definition.reads_active_low:
                invert_mask |= 1 << line_index
            if definition.is_enabled:
                enable_mask |= 1 << line_index
            debounce_milliseconds_per_line[line_index] = definition.debounce_milliseconds

        safe_level_mask = 0
        for output_definition in self.output_lines:
            if output_definition.safe_level_is_high:
                safe_level_mask |= 1 << _numbered(output_definition, "output")

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


def _resolve_one(definition, direction: str, pin_map) -> int:
    """One line's index: what the board says, what the config says, or a refusal.

    `direction` is "in" or "out" and is not decoration -- input line 3 and
    output line 3 are different pins, so a label is only meaningful alongside
    the direction it was written for.
    """
    where = "input" if direction == "in" else "output"
    from_the_board = (
        pin_map.line_index_for_label(direction, definition.pin_label)
        if definition.pin_label
        else None
    )

    if not pin_map.came_from_the_device:
        # An assumed map may fill a gap and may not contradict anybody.
        if definition.line_index is not None:
            return definition.line_index
        if from_the_board is not None:
            return from_the_board
        raise ValueError(
            f"the {where} line {definition.name!r} names pin {definition.pin_label!r}, and this "
            f"board did not say which pins it has -- its firmware is older than the `pins` "
            f"command. Give it a line_index, or flash firmware that answers `pins`"
        )

    if definition.pin_label and from_the_board is None:
        raise ValueError(
            f"the {where} line {definition.name!r} names pin {definition.pin_label!r}, which is "
            f"not {'an input' if direction == 'in' else 'an output'} on this board. It has: "
            f"{pin_map.known_pins(direction)}"
        )

    if definition.line_index is None:
        return from_the_board

    if from_the_board is not None and from_the_board != definition.line_index:
        raise ValueError(
            f"the {where} line {definition.name!r} says line {definition.line_index} and pin "
            f"{definition.pin_label!r}, but this board's {where} line {definition.line_index} is "
            f"pin {pin_map.label_for(direction, definition.line_index)!r} and "
            f"{definition.pin_label!r} is line {from_the_board}. One of the two is wrong, and "
            f"nothing downstream would notice which"
        )

    if definition.line_index >= len(
        pin_map.input_pin_labels if direction == "in" else pin_map.output_pin_labels
    ):
        raise ValueError(
            f"the {where} line {definition.name!r} is line {definition.line_index}, and this "
            f"board has no such {where} line. Its pins are: {pin_map.known_pins(direction)}"
        )
    return definition.line_index


def _numbered(definition, where: str) -> int:
    """A line's index, or a refusal to guess one.

    A line may be configured by pin alone, and until `resolved_against` has
    asked the board there is no index. Everything below this file deals in
    masks, so this is the boundary where "not yet resolved" has to stop being
    representable.
    """
    if definition.line_index is None:
        raise ValueError(
            f"the {where} line {definition.name!r} is configured by pin "
            f"({definition.pin_label!r}) and has not been resolved against a board yet"
        )
    return definition.line_index
