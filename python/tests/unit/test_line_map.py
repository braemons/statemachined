# SPDX-License-Identifier: GPL-3.0-or-later
"""Names on this side, masks on that side, and the wiring in between."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from statemachined.device.device_pin_map import DevicePinMap
from statemachined.model.line_map import LineMap


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


# --------------------------------------------------- resolved against a board ---
#
# docs/reference/protocol.md §3.6. A pin label used to be a comment: free text, checked
# against nothing, and wrong in exactly the way nothing downstream could see.
# Now the board answers with the table its own `pinMode()` was called over, and
# these are the rules for what happens when the config and the board disagree.


def reference_board() -> DevicePinMap:
    return DevicePinMap(
        input_pin_labels=["D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9"],
        output_pin_labels=["D10", "D11", "D12", "A0", "A1", "A2", "A3", "A4"],
        source="device",
    )


def test_a_line_may_be_configured_by_pin_alone():
    """Which is the point: a bit position is not written on the board, and D6 is."""
    resolved = LineMap.model_validate(
        {
            "input_lines": [{"name": "lever", "pin_label": "D6"}],
            "output_lines": [{"name": "valve", "pin_label": "A0"}],
        }
    ).resolved_against(reference_board())
    assert resolved.input_line_index_for_name("lever") == 4
    assert resolved.output_line_index_for_name("valve") == 3


def test_the_two_numberings_are_not_one():
    """Input line 3 and output line 3 are different pins. A resolution that
    looked a label up in the wrong direction is a lever's number driving a
    valve, which is the whole failure mode."""
    board = reference_board()
    assert board.line_index_for_label("in", "D5") == 3
    assert board.line_index_for_label("out", "A0") == 3
    # D5 is not an output at all, however good it looks in an input row.
    assert board.line_index_for_label("out", "D5") is None


def test_a_line_index_that_contradicts_its_pin_is_refused():
    contradiction = LineMap.model_validate(
        {"input_lines": [{"name": "lever", "line_index": 4, "pin_label": "D2"}]}
    )
    with pytest.raises(ValueError) as refused:
        contradiction.resolved_against(reference_board())
    # Both halves, and which is which: "one of these is wrong" is only useful
    # with the board's own answer beside it.
    assert "'D6'" in str(refused.value) and "line 0" in str(refused.value)


def test_a_pin_this_board_does_not_have_is_refused_with_the_ones_it_does():
    with pytest.raises(ValueError) as refused:
        LineMap.model_validate(
            {"input_lines": [{"name": "lever", "pin_label": "A0"}]}
        ).resolved_against(reference_board())
    # A0 exists on this board -- as an output. Naming it as an input is the
    # mistake, and the message has to say what the inputs are.
    assert "not an input" in str(refused.value)
    assert "D2" in str(refused.value)


def test_case_does_not_matter():
    """`a0` and `A0` are the same hole in the board, and refusing a config over
    the difference is pedantry with a soldering iron in the room."""
    resolved = LineMap.model_validate(
        {"output_lines": [{"name": "valve", "pin_label": "a0"}]}
    ).resolved_against(reference_board())
    assert resolved.output_line_index_for_name("valve") == 3


def test_a_line_number_this_board_does_not_have_is_refused():
    with pytest.raises(ValueError) as refused:
        LineMap.model_validate(
            {"input_lines": [{"name": "lever", "line_index": 20}]}
        ).resolved_against(reference_board())
    assert "no such input line" in str(refused.value)


def test_an_assumed_map_fills_gaps_and_overrules_nothing():
    """Firmware older than `pins` leaves the daemon with its own table, which is
    a hand-copied pin map -- the thing the command exists to replace. It may
    help; it may not contradict, because asserting on it is the mistake."""
    assumed = DevicePinMap(["D2", "D3"], ["D10"], source="assumed")
    kept = LineMap.model_validate(
        {"input_lines": [{"name": "lever", "line_index": 7, "pin_label": "D2"}]}
    ).resolved_against(assumed)
    assert kept.input_line_index_for_name("lever") == 7


def test_a_board_that_names_no_pins_cannot_resolve_a_pin_only_line():
    """And says so in terms of what to do about it, since both fixes are the
    reader's to choose between."""
    with pytest.raises(ValueError) as refused:
        LineMap.model_validate(
            {"input_lines": [{"name": "lever", "pin_label": "D6"}]}
        ).resolved_against(DevicePinMap(source="unknown"))
    assert "line_index" in str(refused.value) and "`pins`" in str(refused.value)


def test_a_line_that_says_neither_is_refused_at_the_config():
    """Before any board is involved: there is nothing to resolve from."""
    with pytest.raises(ValidationError) as refused:
        LineMap.model_validate({"input_lines": [{"name": "nowhere"}]})
    assert "neither which line it is nor which pin" in str(refused.value)


def test_an_unresolved_map_refuses_to_produce_masks():
    """The boundary where "not yet resolved" stops being representable. A mask
    built from a missing index would be a wiring command for line zero."""
    with pytest.raises(ValueError) as refused:
        LineMap.model_validate(
            {"input_lines": [{"name": "lever", "pin_label": "D6"}]}
        ).wiring_message_fields()
    assert "has not been resolved" in str(refused.value)
