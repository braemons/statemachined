# SPDX-License-Identifier: GPL-3.0-or-later
"""Whole sessions, end to end, against the firmware built for this machine.

dev/PLAN.md's testing section asks for exactly this and names the cases: whole
trials, cancel races, link loss. What makes them worth running is that the far
end is not a mock -- it is firmware/core, so the refusals are the device's own
refusals, the result is reassembled by the device's own chunker, and a
disagreement between the daemon's idea of the protocol and the firmware's shows
up here rather than on a bench.

The durations are short on purpose. A 60 ms state is long enough to be a real
duration served by the device's own clock and short enough that the suite stays
in the low seconds.
"""

from __future__ import annotations

import time

import pytest

from statemachined.device.device_supervisor import (
    DeviceRefusedTheCommand,
    DeviceSupervisor,
    NoGraphSetCommitted,
    ObservedStateVisit,
)
from statemachined.model.graph_definition import GraphDefinition
from statemachined.model.line_map import LineMap
from statemachined.model.trial_outcome import TrialCancelReason, TrialOutcome

# The socket bridge is part of the daemon and shared with the bench, so there is
# only one of it. The power-cut test needs to start a second device on the same
# store, which the one-per-test fixture cannot do.
from statemachined.device.native_device_on_a_socket import NativeDeviceOnASocket


def bench_line_map() -> LineMap:
    """A rig with nothing plugged into it, which is what a native device is."""
    return LineMap.model_validate(
        {
            "input_lines": [
                {"name": "start_switch", "line_index": 0},
                {"name": "lever", "line_index": 4},
                {"name": "abort", "line_index": 1},
            ],
            "output_lines": [
                {"name": "ready_lamp", "line_index": 0},
                {"name": "reward_valve", "line_index": 3, "safe_level_is_high": True},
            ],
        }
    )


def timed_graph(name: str, milliseconds: int) -> GraphDefinition:
    """Wait --(n ms)--> Hit, raising a lamp for the whole of the wait."""
    return GraphDefinition.model_validate(
        {
            "name": name,
            "entry": "Wait",
            "distributions": {"dwell": {"kind": "fixed", "duration_ms": milliseconds}},
            "states": [
                {
                    "name": "Wait",
                    "on_entry": [{"line": "ready_lamp", "kind": "high"}],
                    "timeout": {"after": "dwell", "goto": "Hit"},
                    "transitions": [{"when": {"all": ["abort"]}, "goto": "Aborted"}],
                },
                {
                    "name": "Hit",
                    "outcome": "HIT",
                    "on_entry": [{"line": "reward_valve", "kind": "pulse", "pulse_ms": 5}],
                },
                {"name": "Aborted", "outcome": "CANCELLED"},
            ],
        }
    )


@pytest.fixture
def supervisor(native_device):
    """A greeted device with this rig's wiring already pushed."""
    observed_visits: list[ObservedStateVisit] = []
    device_supervisor = DeviceSupervisor(
        native_device.target_url,
        bench_line_map(),
        timeout=5.0,
        on_state_visit=observed_visits.append,
    )
    device_supervisor.connect_and_greet()
    device_supervisor.observed_visits = observed_visits  # for the tests to read
    try:
        yield device_supervisor
    finally:
        device_supervisor.disconnect()


# ---------------------------------------------------------------- greeting ---


def test_greeting_reports_a_board_that_holds_nothing_yet(supervisor):
    hello_ack = supervisor.hello_ack
    assert hello_ack["board"] == "native"
    assert hello_ack["proto"] == 1
    assert hello_ack["has_set"] is False
    # The wiring was pushed as part of connecting, before any graph. It is what
    # makes fail_safe() correct for this box.
    assert hello_ack["has_wiring"] is False  # as of the greeting, which precedes it
    assert supervisor.read_state_report()["has_wiring"] is True


def test_the_board_declares_what_it_can_hold_and_the_daemon_believes_it(supervisor):
    assert supervisor.capabilities is not None
    assert supervisor.capabilities.max_graphs >= 1
    assert supervisor.capabilities.max_states >= 2


# ------------------------------------------------------------- whole trials ---


def test_a_whole_trial_runs_and_comes_back_named(supervisor):
    supervisor.upload_graph_set([timed_graph("go-nogo", 60)], set_version=1)
    result = supervisor.run_trial_to_completion(193, "go-nogo", cap_milliseconds=5000)

    assert result.trial_id == 193
    assert result.outcome is TrialOutcome.HIT
    assert not result.path_was_truncated
    # Named, not indexed: Wait then Hit, and the timeout that carried it there.
    assert [visit.state_name for visit in result.visits] == ["Wait", "Hit"]
    assert result.visits[0].exit_cause == "timeout"
    assert result.visits[0].drawn_duration_ms == 60
    # Served by the device's own clock. Generous, because this is a desktop
    # kernel and the point is that the duration is real, not that it is a board.
    assert 55_000 <= result.visits[0].measured_duration_microseconds <= 200_000


def test_every_state_visit_arrives_as_it_happens_and_before_the_result(supervisor):
    supervisor.upload_graph_set([timed_graph("go-nogo", 60)], set_version=1)
    supervisor.run_trial_to_completion(7, "go-nogo", cap_milliseconds=5000)

    visits = supervisor.observed_visits
    assert [observed.visit.state_name for observed in visits] == ["Wait", "Hit"]
    assert [observed.sequence_number for observed in visits] == [0, 1]
    assert all(observed.trial_id == 7 for observed in visits)
    # No ping yet, so there is no correlation and the daemon says so rather than
    # inventing an offset.
    assert visits[0].host_time is None
    assert visits[0].unwrapped_device_microseconds > 0


def test_a_ping_puts_the_visit_stream_into_host_time(supervisor):
    supervisor.upload_graph_set([timed_graph("go-nogo", 60)], set_version=1)
    supervisor.send_heartbeat_ping()
    assert supervisor.clock.has_an_estimate

    host_seconds_before = time.time()
    supervisor.run_trial_to_completion(8, "go-nogo", cap_milliseconds=5000)
    host_seconds_after = time.time()

    observed = supervisor.observed_visits[0]
    assert observed.host_time is not None
    # The estimate lands inside the window the trial actually ran in, which is
    # the only check that does not simply restate the arithmetic.
    assert host_seconds_before <= observed.host_time.host_unix_seconds <= host_seconds_after
    assert observed.host_time.uncertainty_microseconds >= 0


# ------------------------------------------------------------------- sets ---


def test_a_session_switches_graphs_by_naming_them(supervisor):
    # The whole of dev/DAEMON.md 3.2, end to end: both graphs go up once, and
    # then a trial names one. Nothing is uploaded between the two trials.
    supervisor.upload_graph_set(
        [timed_graph("go-nogo", 60), timed_graph("2afc", 120)], set_version=4
    )
    assert supervisor.committed_graph_set is not None
    assert supervisor.committed_graph_set.slot_for_graph_name("2afc") == 1

    short = supervisor.run_trial_to_completion(1, "go-nogo", cap_milliseconds=5000)
    long = supervisor.run_trial_to_completion(2, "2afc", cap_milliseconds=5000)

    assert short.visits[0].drawn_duration_ms == 60
    assert long.visits[0].drawn_duration_ms == 120
    assert short.outcome is TrialOutcome.HIT and long.outcome is TrialOutcome.HIT


def test_a_trial_before_any_upload_is_refused_by_the_daemon(supervisor):
    with pytest.raises(NoGraphSetCommitted):
        supervisor.configure_trial(1, "go-nogo")


def test_a_set_too_big_for_the_board_is_refused_before_anything_is_sent(supervisor):
    # Compiled against the caps the board declared, so this is the real number
    # rather than one written down here.
    assert supervisor.capabilities is not None
    too_many = supervisor.capabilities.max_graphs + 1
    with pytest.raises(Exception, match="graphs and the board holds"):
        supervisor.upload_graph_set(
            [timed_graph(f"graph-{index}", 20) for index in range(too_many)], set_version=9
        )
    assert supervisor.committed_graph_set is None


# ------------------------------------------------------------ cancel races ---


def test_a_cancel_stops_a_running_trial_and_says_why(supervisor):
    supervisor.upload_graph_set([timed_graph("go-nogo", 5000)], set_version=1)
    supervisor.configure_trial(11, "go-nogo", cap_milliseconds=10000)
    supervisor.start_trial(11)

    cancel_ack = supervisor.cancel_trial(11)
    assert cancel_ack["cancelled"] is True

    result = supervisor.wait_for_trial_result()
    assert result.outcome is TrialOutcome.CANCELLED
    assert result.cancel_reason is TrialCancelReason.HOST
    # The path up to the cut is still reported: a cancelled trial is recorded
    # rather than dropped, so a gap in the numbering never has to be explained.
    assert result.visits[-1].exit_cause == "cancel"


def test_a_cancel_that_loses_the_race_gets_the_real_outcome_back(supervisor):
    # The device's answer, passed through unchanged. Asking to cancel and being
    # told HIT is the caller's to cope with; the alternative is a record
    # claiming a trial was cancelled when the animal had already responded.
    supervisor.upload_graph_set([timed_graph("go-nogo", 20)], set_version=1)
    result = supervisor.run_trial_to_completion(12, "go-nogo", cap_milliseconds=5000)
    assert result.outcome is TrialOutcome.HIT

    with pytest.raises(DeviceRefusedTheCommand) as refusal:
        supervisor.cancel_trial(12)
    assert refusal.value.code == "unknown_trial"


# -------------------------------------------------------------- link loss ---


def test_a_reconnect_does_not_cost_a_re_upload(supervisor, native_device):
    # The committed set survives a reconnect, which is what hello_ack's has_set
    # and set_version are for. A bridge restarting must not cost a session its
    # paradigms.
    supervisor.upload_graph_set([timed_graph("go-nogo", 60)], set_version=5)
    set_version_before = supervisor.committed_graph_set.set_version

    native_device.drop_the_link()
    hello_ack = supervisor.reconnect_and_restore()

    assert supervisor.connection_count == 2
    assert hello_ack["has_set"] is True
    assert hello_ack["set_version"] == set_version_before
    # And it still runs, without anything having been uploaded again.
    result = supervisor.run_trial_to_completion(21, "go-nogo", cap_milliseconds=5000)
    assert result.outcome is TrialOutcome.HIT


def test_a_reconnect_starts_a_new_session_seed_and_forgets_the_clock(supervisor, native_device):
    # A seed is per session and a `hello` is what opens one. The clock offset
    # goes with it: a device that had reset would restart its clock from zero,
    # and carrying an offset across would make every later timestamp wrong by
    # however long the board was away -- and wrong plausibly.
    supervisor.send_heartbeat_ping()
    assert supervisor.clock.has_an_estimate
    seed_before = supervisor.session_seed

    native_device.drop_the_link()
    supervisor.reconnect_and_restore()

    assert supervisor.session_seed != seed_before
    assert not supervisor.clock.has_an_estimate


def test_a_fixed_seed_survives_a_reconnect_when_one_was_configured(native_device):
    # The other half of the same decision: a session that was given a seed keeps
    # it, so a reconnect mid-session does not change what trial 412 draws.
    supervisor = DeviceSupervisor(
        native_device.target_url, bench_line_map(), timeout=5.0, session_seed="0123456789ABCDEF"
    )
    supervisor.connect_and_greet()
    try:
        native_device.drop_the_link()
        supervisor.reconnect_and_restore()
        assert supervisor.session_seed == "0123456789ABCDEF"
    finally:
        supervisor.disconnect()


# ------------------------------------------------- the board on its own ---


def self_driving_graph(name: str, milliseconds: int, dwell_ms: int) -> GraphDefinition:
    """The same trial, with the terminal state declaring how long to hold before
    another may start."""
    definition = timed_graph(name, milliseconds).model_dump()
    definition["distributions"]["iti"] = {"kind": "fixed", "duration_ms": dwell_ms}
    for state in definition["states"]:
        if state["name"] == "Hit":
            state["relight_after"] = "iti"
    return GraphDefinition.model_validate(definition)


def test_a_board_told_to_drive_itself_runs_trial_after_trial(supervisor):
    """No configure, no start: the device is the authority because nothing else
    is there to be one. dev/PROTOCOL.md 3.7."""
    supervisor.upload_graph_set([self_driving_graph("shaping", 40, 20)], set_version=1)
    reply = supervisor.set_autorun(True, graph_name="shaping", first_trial_id=500)
    assert reply["enabled"] is True
    assert reply["active"] is True

    results = []
    for _ in range(3):
        results.append(supervisor.wait_for_trial_result(timeout=5.0))
    assert [r.trial_id for r in results] == [500, 501, 502]
    assert all(r.outcome is TrialOutcome.HIT for r in results)

    supervisor.set_autorun(False)
    assert supervisor.read_autorun()["active"] is False


def test_a_host_that_greets_takes_the_rig(supervisor, native_device):
    """The stored setting survives; the driving stops. A daemon that crashed must
    not leave a board rewarding an animal nobody is watching."""
    supervisor.upload_graph_set([self_driving_graph("shaping", 40, 20)], set_version=1)
    supervisor.set_autorun(True, graph_name="shaping")
    supervisor.wait_for_trial_result(timeout=5.0)

    supervisor.disconnect()
    supervisor.connect_and_greet()
    autorun = supervisor.read_autorun()
    assert autorun["enabled"] is True  # the setting is still there
    assert autorun["active"] is False  # and it is not driving any more
    assert supervisor.read_state_report()["autorun"] is False


def started_trial_ids(messages: list[dict]) -> list[int]:
    """The trials a watching daemon saw end, in order."""
    return [
        int(m["trial_id"])
        for m in messages
        if m.get("msg_type") == "result_begin" and "trial_id" in m
    ]


def test_a_board_comes_back_from_a_power_cut_running_what_it_was_told_to_run(
    native_device, tmp_path
):
    """The whole of standalone operation, against the firmware's own binary:
    upload, arm autorun, save, kill the device, start it again on the same store
    -- and it comes up running trials with nobody having said hello."""
    store = str(tmp_path / "restored-store.bin")
    first = NativeDeviceOnASocket(store_path=store)
    first.start()
    try:
        supervisor = DeviceSupervisor(first.target_url, bench_line_map(), timeout=5.0)
        supervisor.connect_and_greet()
        supervisor.push_wiring()
        supervisor.upload_graph_set([self_driving_graph("shaping", 40, 20)], set_version=3)
        supervisor.set_autorun(True, graph_name="shaping", first_trial_id=900)
        saved = supervisor.save_settings()
        assert saved["has_set"] is True
        assert saved["autorun"] is True
        assert saved["write_count"] == 1
        supervisor.disconnect()
    finally:
        first.stop()

    # The power cut. Same store, new process, and nothing greets it until after
    # it has already been running.
    second = NativeDeviceOnASocket(store_path=store)
    second.start()
    try:
        # Connect without greeting, so the board keeps driving itself while the
        # daemon merely listens -- which is what a bystander sees. It cannot
        # *name* what it sees: naming a result needs the graph, and this daemon
        # has not been given one on this connection. The trial ids are enough to
        # prove the board is running on its own.
        seen: list[dict] = []
        after = DeviceSupervisor(
            second.target_url,
            bench_line_map(),
            timeout=5.0,
            on_unsolicited_message=seen.append,
        )
        after.connect_and_watch()
        deadline = time.time() + 5.0
        while len(started_trial_ids(seen)) < 2 and time.time() < deadline:
            after.pump_incoming_lines(budget_seconds=0.05)
        assert started_trial_ids(seen)[:2] == [900, 901]

        # Only now does anybody greet, and that takes the rig.
        after.connect_and_greet()
        assert after.read_state_report()["autorun"] is False
        assert after.read_autorun()["enabled"] is True
        after.disconnect()
    finally:
        second.stop()
