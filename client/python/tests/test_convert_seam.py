# SPDX-License-Identifier: LGPL-3.0-or-later
"""The seam, with no daemon and no network.

Everything here is about one thing: **the generated types and `api_types` say
the same thing**. It is the cheapest place to catch a mistranslation and the
only place some of these cases can be produced at all — a daemon that is
working correctly never sends a trace entry with an unparseable timestamp in
it, and this is where that is not a reason to leave the behaviour untested.
"""

from __future__ import annotations

import datetime as dt

import grpc
import pytest
from statemachined_client import (
    DaemonIsUnavailable,
    DaemonRefusedTheRequest,
    DistributionPatch,
    NoBoardIsAttached,
    NoSuchDocument,
    RigConfigurationPatch,
    TheRigIsNotInAStateForThat,
    TrialCancelReason,
    TrialOutcome,
)
from statemachined_client import _wire_conversions as convert
from statemachined_client._grpc_transport import REFUSAL_METADATA_KEY, refusal_of
from statemachined_client._proto.statemachined.v1 import (
    common_pb2,
    device_pb2,
    recording_pb2,
    session_pb2,
    state_pb2,
    trial_pb2,
)

# -- the enums ------------------------------------------------------------------


@pytest.mark.parametrize("reason", list(TrialCancelReason))
def test_every_cancel_reason_survives_the_round_trip(reason):
    assert convert.cancel_reason_from_wire(convert.cancel_reason_to_wire(reason)) is reason


@pytest.mark.parametrize("outcome", list(TrialOutcome))
def test_every_outcome_survives_the_round_trip(outcome):
    assert convert.trial_outcome_from_wire(convert.trial_outcome_to_wire(outcome)) is outcome


def test_the_outcome_codes_are_the_tdr_codes():
    """Not a tautology: these numbers are in every `.tdr` the lab has written.

    They are shared with triald and with the firmware, and renumbering one
    would silently reinterpret files nobody is going to re-run.
    """
    assert TrialOutcome.UNDETERMINED == -1
    assert TrialOutcome.HIT == 1
    assert TrialOutcome.CANCELLED == 10


def test_a_cancel_reason_the_client_has_never_heard_of_is_refused_loudly():
    """At the seam, where a mistranslation is still cheap.

    The alternative — falling back to `NONE` — would report a trial that was
    cancelled by a reason this client does not know as one that ran to the end.
    """
    with pytest.raises(KeyError):
        convert.cancel_reason_from_wire(999)


# -- absent is not zero ---------------------------------------------------------


def test_an_unset_trial_id_is_none_and_not_trial_zero():
    """The whole reason `_maybe` exists. Trial 0 is a trial."""
    assert convert.rig_state_from_wire(state_pb2.RigState()).trial_id is None
    assert convert.rig_state_from_wire(state_pb2.RigState(trial_id=0)).trial_id == 0


def test_an_unset_message_field_is_none_and_not_an_empty_record():
    """A `DeviceState` with no capacities has not reported any.

    A default-constructed `DeviceCapacities` would say `max_states=0`, which is
    a board that can hold no states — a different and quite specific claim.
    """
    assert convert.device_state_from_wire(device_pb2.DeviceState()).capacities is None

    present = device_pb2.DeviceState()
    present.capacities.SetInParent()
    assert convert.device_state_from_wire(present).capacities is not None


def test_an_unset_optional_bool_is_none_and_not_false():
    """`has_wiring` has three answers and `False` is only one of them."""
    assert convert.device_state_from_wire(device_pb2.DeviceState()).has_wiring is None
    assert (
        convert.device_state_from_wire(device_pb2.DeviceState(has_wiring=False)).has_wiring
        is False
    )


def test_nothing_lost_is_none_and_not_entry_zero():
    """`lost_entries_before` is the field a consumer reads to know it has a hole."""
    assert convert.trace_window_from_wire(state_pb2.TraceWindow()).lost_entries_before is None
    window = state_pb2.TraceWindow(lost_entries_before=0)
    assert convert.trace_window_from_wire(window).lost_entries_before == 0


def test_a_session_that_has_never_opened_has_no_open_seconds():
    """Zero seconds open is a session that just opened, which is not this."""
    assert convert.session_state_from_wire(session_pb2.SessionState()).open_seconds is None
    just_opened = session_pb2.SessionState(open_seconds=0.0)
    assert convert.session_state_from_wire(just_opened).open_seconds == 0.0


# -- the host times -------------------------------------------------------------


def test_a_host_time_arrives_as_a_datetime():
    entry = state_pb2.TraceEntry(recorded_host_time="2026-09-20T11:22:33.500000+00:00")
    recorded = convert.trace_entry_from_wire(entry).recorded_host_time
    assert recorded == dt.datetime(2026, 9, 20, 11, 22, 33, 500000, tzinfo=dt.UTC)


def test_a_host_time_spelled_with_z_arrives_as_a_datetime():
    entry = state_pb2.TraceEntry(recorded_host_time="2026-09-20T11:22:33Z")
    assert convert.trace_entry_from_wire(entry).recorded_host_time is not None


def test_an_unreadable_host_time_is_none_rather_than_an_exception():
    """A trace with one odd stamp in it is still a trace worth reading.

    These come off `.jsonl` files that an older daemon may have written, and
    refusing the whole entry would make the record unreadable for the sake of
    one field nothing branches on.
    """
    entry = state_pb2.TraceEntry(recorded_host_time="the day before yesterday")
    assert convert.trace_entry_from_wire(entry).recorded_host_time is None
    assert convert.trace_entry_from_wire(state_pb2.TraceEntry()).recorded_host_time is None


def test_a_recording_segment_carries_both_of_its_times():
    segment = recording_pb2.RecordingSegment(
        started_host_time="2026-09-20T10:00:00+00:00",
        ended_host_time="2026-09-20T10:05:00+00:00",
    )
    converted = convert.recording_segment_from_wire(segment)
    assert converted.started_host_time is not None
    assert converted.ended_host_time is not None
    assert converted.ended_host_time - converted.started_host_time == dt.timedelta(minutes=5)


# -- the trace payload ----------------------------------------------------------


def test_a_payload_number_that_was_an_int_comes_back_an_int():
    """Struct has one number type and it is a double.

    Without this, every consumer of a trace payload writes
    `int(payload["state_index"])` and one of them forgets.
    """
    entry = state_pb2.TraceEntry(kind="state_visit")
    entry.payload.update({"state_index": 4, "measured_duration_microseconds": 12345})
    payload = convert.trace_entry_from_wire(entry).payload
    assert payload["state_index"] == 4
    assert isinstance(payload["state_index"], int)
    assert isinstance(payload["measured_duration_microseconds"], int)


def test_a_payload_number_that_was_a_float_stays_a_float():
    entry = state_pb2.TraceEntry(kind="state_visit")
    entry.payload.update({"reaction_time_seconds": 0.25})
    assert convert.trace_entry_from_wire(entry).payload["reaction_time_seconds"] == 0.25


def test_a_nested_payload_is_converted_all_the_way_down():
    entry = state_pb2.TraceEntry(kind="something_nested")
    entry.payload.update({"outer": {"inner": [1, 2, {"deep": 3}]}})
    payload = convert.trace_entry_from_wire(entry).payload
    assert payload["outer"]["inner"] == [1, 2, {"deep": 3}]
    assert isinstance(payload["outer"]["inner"][2]["deep"], int)


def test_a_kind_this_client_has_never_heard_of_still_arrives():
    """The ring holds entries a newer daemon wrote. They are handed back."""
    entry = state_pb2.TraceEntry(kind="a_kind_invented_next_month", entry_number=7)
    converted = convert.trace_entry_from_wire(entry)
    assert converted.kind == "a_kind_invented_next_month"
    assert converted.entry_number == 7


# -- what travels outward -------------------------------------------------------


def test_a_distribution_patch_sets_only_what_it_names():
    """`minimum_ms=0` is a distribution that can draw nothing.

    A patch that turned "leave it" into zero would be a different experiment.
    """
    message = convert.distribution_patch_to_wire(DistributionPatch("foreperiod", mean_ms=900))
    assert message.name == "foreperiod"
    assert message.HasField("mean_ms")
    assert not message.HasField("minimum_ms")


def test_a_distribution_patch_can_set_a_zero():
    message = convert.distribution_patch_to_wire(DistributionPatch("x", minimum_ms=0))
    assert message.HasField("minimum_ms")
    assert message.minimum_ms == 0


def test_a_rig_config_patch_can_clear_a_string():
    """`expected_board=""` is "accept any board", and has to be reachable."""
    message = convert.rig_configuration_patch_to_wire(RigConfigurationPatch(expected_board=""))
    assert message.HasField("expected_board")
    assert not message.HasField("device_target")


def test_an_empty_rig_config_patch_sets_nothing():
    message = convert.rig_configuration_patch_to_wire(RigConfigurationPatch())
    assert message.ListFields() == []


# -- refusals -------------------------------------------------------------------


class _Failure(grpc.RpcError):
    """A gRPC failure, as grpcio hands one to a caller.

    Written here rather than provoked from a daemon because the point is the
    classification, and the classification has to be right for statuses this
    suite cannot make a healthy daemon produce.
    """

    def __init__(self, code, details="", trailers=()):
        self._code = code
        self._details = details
        self._trailers = trailers

    def code(self):
        return self._code

    def details(self):
        return self._details

    def trailing_metadata(self):
        return self._trailers


def _typed(error: str, detail: str, context: str = ""):
    body = common_pb2.Error(error=error, detail=detail, context=context)
    return ((REFUSAL_METADATA_KEY, body.SerializeToString()),)


def test_a_typed_refusal_arrives_with_the_daemons_own_words():
    refusal = refusal_of(
        _Failure(
            grpc.StatusCode.NOT_FOUND,
            "no graph named go-nogo (graph_name)",
            _typed("no_such_graph", "no graph named go-nogo", "graph_name"),
        )
    )
    assert isinstance(refusal, NoSuchDocument)
    assert refusal.error == "no_such_graph"
    assert refusal.context == "graph_name"
    assert refusal.detail == "no graph named go-nogo"
    assert not refusal.retryable


def test_a_wrong_moment_gets_its_own_class():
    refusal = refusal_of(
        _Failure(
            grpc.StatusCode.FAILED_PRECONDITION,
            "no config is loaded",
            _typed("no_state_machine_config_loaded", "no config is loaded", "config"),
        )
    )
    assert isinstance(refusal, TheRigIsNotInAStateForThat)
    assert not refusal.retryable, "the fix is to load one, not to try again"


def test_unavailable_with_a_typed_refusal_is_the_board_and_not_the_daemon():
    """The split that makes `unavailable` worth reading.

    A typed refusal means the daemon answered, which means the daemon is up.
    """
    refusal = refusal_of(
        _Failure(
            grpc.StatusCode.UNAVAILABLE,
            "no board is connected (device)",
            _typed("not_connected", "no board is connected", "device"),
        )
    )
    assert isinstance(refusal, NoBoardIsAttached)
    assert not isinstance(refusal, DaemonIsUnavailable)
    assert refusal.context == "device"


def test_unavailable_with_nothing_attached_is_silence():
    refusal = refusal_of(
        _Failure(grpc.StatusCode.UNAVAILABLE, "failed to connect to all addresses")
    )
    assert isinstance(refusal, DaemonIsUnavailable)
    assert refusal.retryable, "a daemon that is starting is worth waiting for"


def test_a_status_this_client_has_no_class_for_is_still_a_refusal():
    refusal = refusal_of(
        _Failure(grpc.StatusCode.INTERNAL, "it broke", _typed("internal", "it broke"))
    )
    assert type(refusal) is DaemonRefusedTheRequest
    assert refusal.error == "internal"


def test_a_refusal_that_cannot_be_decoded_is_still_a_refusal():
    """A client that raised on an unreadable trailer would fail at exactly the
    moment somebody needed to read the message."""
    refusal = refusal_of(
        _Failure(
            grpc.StatusCode.NOT_FOUND, "something", ((REFUSAL_METADATA_KEY, b"\xff\xff\xff"),)
        )
    )
    assert isinstance(refusal, NoSuchDocument)
    assert refusal.detail == "something"


def test_a_refusal_reads_as_a_sentence():
    refusal = refusal_of(
        _Failure(
            grpc.StatusCode.NOT_FOUND,
            "",
            _typed("no_such_graph", "no graph named go-nogo", "graph_name"),
        )
    )
    assert str(refusal) == "no_such_graph: no graph named go-nogo (change: graph_name)"


# -- the addresses --------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("", "127.0.0.1:8082"),
        ("rig-3.local", "rig-3.local:8082"),
        ("rig-3.local:9999", "rig-3.local:9999"),
        ("http://rig-3.local/", "rig-3.local:8082"),
        ("[::1]:9999", "[::1]:9999"),
    ],
)
def test_an_address_becomes_a_grpc_target(given, expected):
    from statemachined_client.daemon_client import DEFAULT_PORT, _target

    assert _target(given, DEFAULT_PORT) == expected


def test_a_visit_without_a_fired_transition_says_so():
    visit = trial_pb2.StateVisit(state_name="Late", exit_cause="timeout")
    converted = convert.state_visit_from_wire(visit)
    assert converted.fired_transition_position is None
    assert converted.fired_transition_target_state_name is None
