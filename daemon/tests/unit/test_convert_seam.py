# SPDX-License-Identifier: GPL-3.0-or-later
"""The seam between this daemon's types and the wire's.

**What is checked here is what a round trip against a live daemon cannot
catch**: a field that is absent rather than zero, a key a board did not send,
an enum value nobody chose a wire number for. A daemon answering with its own
defaults agrees with a seam that invented the same ones, which is why those
tests would pass either way and these would not.
"""

from __future__ import annotations

import pytest

from statemachined._proto.statemachined.v1 import trial_pb2
from statemachined.daemon.api import convert
from statemachined.daemon.api.convert.trial import Refused, outcome_name
from statemachined.model.line_map import InputLineDefinition, LineMap, OutputLineDefinition
from statemachined.model.trial_outcome import TrialCancelReason, TrialOutcome
from statemachined.model.trial_record import StateVisitRecord, TrialResultRecord


def a_visit(**overrides) -> StateVisitRecord:
    return StateVisitRecord(
        **{
            "state_name": "Foreperiod",
            "exit_cause": "timeout",
            "drawn_duration_ms": 100,
            "entered_device_microseconds": 1_000,
            "measured_duration_microseconds": 101_000,
            **overrides,
        }
    )


# -- presence -------------------------------------------------------------------


def test_transition_zero_is_not_the_same_as_no_transition() -> None:
    """The failure this exists to stop: an exit that was not a transition read
    as "the first transition fired", which is a different trial."""
    fired = convert.state_visit_to_wire(
        a_visit(exit_cause="transition", fired_transition_position=0)
    )
    assert fired.HasField("fired_transition_position")
    assert fired.fired_transition_position == 0

    timed_out = convert.state_visit_to_wire(a_visit())
    assert not timed_out.HasField("fired_transition_position")


def test_an_unresolved_transition_target_stays_absent() -> None:
    """An index the graph cannot explain is a finding — a row from a device
    holding a different set — and a plausible name for it would bury that."""
    visit = convert.state_visit_to_wire(
        a_visit(fired_transition_position=2, fired_transition_target_state_name=None)
    )
    assert visit.HasField("fired_transition_position")
    assert not visit.HasField("fired_transition_target_state_name")


def test_a_patch_that_sets_zero_is_a_patch() -> None:
    """Zero is a legal duration, so presence is the question and truthiness is
    the bug."""
    request = trial_pb2.ConfigureTrialRequest(trial_id=1)
    request.distribution_patches.add(name="foreperiod", minimum_ms=0)
    decoded = convert.configure_trial_from_wire(request)
    assert decoded["distribution_patches"] == [{"name": "foreperiod", "minimum_ms": 0}]


def test_a_patch_carries_only_what_was_set() -> None:
    request = trial_pb2.ConfigureTrialRequest(trial_id=1)
    request.distribution_patches.add(name="foreperiod", mean_ms=250)
    assert convert.configure_trial_from_wire(request)["distribution_patches"] == [
        {"name": "foreperiod", "mean_ms": 250}
    ]


def test_start_line_zero_is_a_line() -> None:
    """Input line 0 is a real line, which is why the field is `optional`."""
    request = trial_pb2.ConfigureTrialRequest(trial_id=1, start_source="line", start_line=0)
    assert convert.configure_trial_from_wire(request)["start_line"] == 0

    without = trial_pb2.ConfigureTrialRequest(trial_id=1)
    assert convert.configure_trial_from_wire(without)["start_line"] is None


def test_an_empty_start_source_takes_the_default() -> None:
    # proto3 cannot tell "" from unset for a string, so the seam decides once.
    assert (
        convert.configure_trial_from_wire(trial_pb2.ConfigureTrialRequest(trial_id=1))[
            "start_source"
        ]
        == "serial"
    )


# -- refusals -------------------------------------------------------------------


def test_a_patch_with_no_name_is_refused_by_field() -> None:
    request = trial_pb2.ConfigureTrialRequest(trial_id=1)
    request.distribution_patches.add(minimum_ms=10)
    with pytest.raises(Refused) as refusal:
        convert.configure_trial_from_wire(request)
    assert refusal.value.context == "name"


def test_a_patch_that_sets_nothing_is_refused() -> None:
    # Not a no-op: somebody meant to override something and the message did not
    # carry it, which is worth saying rather than arming a trial as if they had
    # not asked.
    request = trial_pb2.ConfigureTrialRequest(trial_id=1)
    request.distribution_patches.add(name="foreperiod")
    with pytest.raises(Refused) as refusal:
        convert.configure_trial_from_wire(request)
    assert refusal.value.context == "distribution_patches"


def test_a_negative_trial_id_is_refused() -> None:
    with pytest.raises(Refused) as refusal:
        convert.configure_trial_from_wire(trial_pb2.ConfigureTrialRequest(trial_id=-1))
    assert refusal.value.context == "trial_id"


# -- the taxonomy ---------------------------------------------------------------


@pytest.mark.parametrize("reason", list(TrialCancelReason))
def test_every_cancel_reason_has_a_wire_value(reason: TrialCancelReason) -> None:
    """Read off the enum rather than listed, so a reason added to the model and
    forgotten at the seam fails instead of being recorded as `NONE`."""
    record = TrialResultRecord(trial_id=1, outcome=TrialOutcome.CANCELLED, cancel_reason=reason)
    assert convert.trial_result_to_wire(record).cancel_reason == reason.value


def test_the_outcome_crosses_as_its_tdr_code() -> None:
    record = TrialResultRecord(trial_id=1, outcome=TrialOutcome.UNEXPECTED_START_SIGNAL)
    assert convert.trial_result_to_wire(record).outcome == 8


def test_an_outcome_code_this_build_has_never_seen_is_kept() -> None:
    """A newer board or a newer triald, not a broken one: the taxonomy grows by
    addition, and reading an unknown code as `NOT_STARTED` would record a trial
    that never happened."""
    assert outcome_name(1) == "HIT"
    assert outcome_name(99) == "99"


# -- what a board did not send --------------------------------------------------


def test_a_state_report_with_nothing_in_it_still_describes_a_rig() -> None:
    """A board older than this daemon does not send every counter. Refusing to
    describe the rig over that would be a version gate against firmware."""
    message = convert.device_state_to_wire(
        connected=False,
        target="loop://",
        hello_ack=None,
        state_report=None,
        capabilities=None,
        committed=None,
        pin_labels_came_from="assumed",
        connection_count=0,
        last_error="",
    )
    assert message.connected is False
    assert message.scan.hz == 0
    assert not message.HasField("capacities")
    assert not message.HasField("committed_set")


def test_no_board_means_no_capacities_rather_than_zeroed_ones() -> None:
    """A board with `max_states = 0` and a board that has not greeted are
    different situations, and only one of them means a graph will be refused."""
    message = convert.device_state_to_wire(
        connected=False,
        target="",
        hello_ack={},
        state_report={},
        capabilities=None,
        committed=None,
        pin_labels_came_from="assumed",
        connection_count=0,
        last_error="",
    )
    assert not message.HasField("capacities")


def test_has_wiring_has_three_answers() -> None:
    def wiring(state_report, hello_ack):
        return convert.device_state_to_wire(
            connected=True,
            target="",
            hello_ack=hello_ack,
            state_report=state_report,
            capabilities=None,
            committed=None,
            pin_labels_came_from="device",
            connection_count=1,
            last_error="",
        )

    assert wiring({"has_wiring": True}, {}).has_wiring is True
    assert wiring({"has_wiring": False}, {}).has_wiring is False
    # Nothing has said, which is not "no".
    assert not wiring({}, {}).HasField("has_wiring")
    # The greeting answers when the state report does not.
    assert wiring({}, {"has_wiring": True}).has_wiring is True


def test_the_scan_counters_come_through_by_name() -> None:
    message = convert.scan_health_to_wire(
        {"hz": 9871, "overruns": 4, "worst_gap": 2, "tx_stalls": 7}
    )
    assert (message.hz, message.overruns, message.worst_gap, message.tx_stalls) == (
        9871,
        4,
        2,
        7,
    )


# -- the lines ------------------------------------------------------------------


def a_map() -> LineMap:
    return LineMap(
        input_lines=[
            InputLineDefinition(name="lever", line_index=3),
            InputLineDefinition(name="unwired", pin_label="D7"),
        ],
        output_lines=[OutputLineDefinition(name="valve", line_index=1)],
    )


def test_a_line_reads_its_bit_of_the_device_word() -> None:
    view = convert.line_map_view_to_wire(
        a_map(),
        input_word=0b1000,
        output_word=0b0000,
        pin_labels_came_from="device",
        board_input_pins=["D0"],
        board_output_pins=["D1"],
    )
    assert view.input_lines[0].is_high_now is True
    assert view.output_lines[0].is_high_now is False


def test_a_line_with_no_index_has_no_level_and_no_index() -> None:
    """A line configured by pin, with no board to resolve it against: there is
    no bit to read, and inventing one would be a wiring claim."""
    view = convert.line_map_view_to_wire(
        a_map(),
        input_word=0xFFFF,
        output_word=0,
        pin_labels_came_from="assumed",
        board_input_pins=[],
        board_output_pins=[],
    )
    unwired = view.input_lines[1]
    assert not unwired.HasField("line_index")
    assert not unwired.HasField("is_high_now")


def test_nothing_attached_means_no_level_at_all() -> None:
    """Low and "nobody asked the board" are the difference between a wiring
    fault and a disconnected cable."""
    view = convert.line_map_view_to_wire(
        a_map(),
        input_word=None,
        output_word=None,
        pin_labels_came_from="assumed",
        board_input_pins=[],
        board_output_pins=[],
    )
    assert view.input_lines[0].line_index == 3
    assert not view.input_lines[0].HasField("is_high_now")
