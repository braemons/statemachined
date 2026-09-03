# SPDX-License-Identifier: GPL-3.0-or-later
"""Names on this side, masks on that side, and the wiring in between."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from statemachined.model.lines import LineMap


def rig_line_map() -> LineMap:
    return LineMap.model_validate(
        {
            "input_lines": [
                {"name": "start_switch", "line_index": 0, "debounce_milliseconds": 20},
                {"name": "abort", "line_index": 1, "reads_active_low": True},
                {"name": "lever_left", "line_index": 4, "debounce_milliseconds": 5},
                {"name": "lever_right", "line_index": 5, "is_enabled": False},
            ],
            "output_lines": [
                {"name": "ready_lamp", "line_index": 0},
                {"name": "reward_valve", "line_index": 3, "safe_level_is_high": True},
            ],
        }
    )


def test_a_name_becomes_a_bit_position():
    assert rig_line_map().input_line_index_for_name("lever_left") == 4
    assert rig_line_map().output_line_index_for_name("reward_valve") == 3


def test_an_unknown_name_lists_what_this_rig_has():
    # The failure is a paradigm moved between rigs, and the useful answer is
    # what the new rig calls things.
    with pytest.raises(ValueError, match="no input line is called 'paw'.*lever_left"):
        rig_line_map().input_line_index_for_name("paw")


def test_a_predicate_over_several_names_becomes_one_mask():
    # `all` over a set of lines is a single `and` on the device, evaluated in
    # the scan -- which is why order does not matter and repetition is harmless.
    line_map = rig_line_map()
    assert line_map.input_line_mask_for_names(["lever_left", "lever_right"]) == (1 << 4) | (1 << 5)
    assert line_map.input_line_mask_for_names(["lever_right", "lever_left"]) == (1 << 4) | (1 << 5)
    assert line_map.input_line_mask_for_names(["abort", "abort"]) == 1 << 1


def test_two_names_for_one_line_are_refused():
    # Not a harmless alias: a graph naming both would raise one line and believe
    # it had raised two, and the mistake is invisible in the record.
    with pytest.raises(ValidationError, match="a second name for it"):
        LineMap.model_validate(
            {
                "input_lines": [
                    {"name": "lever", "line_index": 4},
                    {"name": "lever_left", "line_index": 4},
                ]
            }
        )


def test_two_lines_with_the_same_name_are_refused():
    with pytest.raises(ValidationError, match="two output lines are called 'lamp'"):
        LineMap.model_validate(
            {
                "output_lines": [
                    {"name": "lamp", "line_index": 0},
                    {"name": "lamp", "line_index": 1},
                ]
            }
        )


def test_an_input_and_an_output_may_share_a_line_number():
    # They are separate index spaces on the device: input 0 and output 0 are
    # different pins, and refusing this would be inventing a constraint the
    # firmware does not have.
    LineMap.model_validate(
        {
            "input_lines": [{"name": "start_switch", "line_index": 0}],
            "output_lines": [{"name": "ready_lamp", "line_index": 0}],
        }
    )


# --------------------------------------------------------------- wiring ---


def test_the_wiring_command_carries_what_the_rig_did_to_each_line():
    fields = rig_line_map().wiring_message_fields()
    assert fields["invert"] == 1 << 1  # abort is active-low
    assert fields["enable"] == (1 << 0) | (1 << 1) | (1 << 4)  # lever_right is not wired
    assert fields["safe"] == 1 << 3  # the valve is held open by a low, so safe is high


def test_debounce_is_reported_by_line_index_and_trimmed():
    # Index = line, so a gap is a zero rather than a shorter list meaning
    # something else. The trailing zeros go because the device fills the rest
    # with zeros anyway and 32 entries of nothing is most of a line's budget.
    assert rig_line_map().wiring_message_fields()["debounce_ms"] == [20, 0, 0, 0, 5]


def test_an_empty_line_map_still_produces_a_whole_wiring():
    # Every field, always: the command replaces what the board holds, so a
    # partial one would leave a board moved between rigs carrying half of each.
    fields = LineMap().wiring_message_fields()
    assert fields == {"invert": 0, "enable": 0, "safe": 0, "debounce_ms": []}
