# SPDX-License-Identifier: GPL-3.0-or-later
"""The whole daemon, over gRPC, against the firmware built for this machine.

Everything below the API has been tested on its own by now. What this adds is
the thing only the assembled daemon can be wrong about: that a graph put in the
store by one call is uploadable by another, that a trial named over the wire
reaches the device and comes back named, that a result nobody asked for lands
in the trace, and that the link thread and an rpc can both want the port
without deadlocking.

The last one is why these run against a real device rather than a stub. A lock
held across a serial read is exactly the kind of thing that works in a test
with no I/O in it.

**This was `test_http_api_against_native_device.py`**, driving the routes with
an `httpx` client over an in-process ASGI app. The routes are gone; the
interface is `proto/statemachined/v1/`, and what drives it here is
`statemachined-client` — the published one, over a real channel, on a loopback
port. Its companion `test_configuring_the_rig.py` covers setting a rig up; this
one covers the parts that only exist because there is a board on the other end.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from grpc_harness import DaemonOnALoopbackPort
from rig_harness import (
    BENCH_LINE_MAP,
    configuration_for,
    timed_graph,
    wait_until,
)
from statemachined_client import (
    KIND_STATE_VISIT,
    KIND_TRIAL_RESULT,
    DaemonRefusedTheRequest,
    NoSuchDocument,
    TheRigIsNotInAStateForThat,
    TrialCancelReason,
    TrialOutcome,
)

#: How long a trial gets. Generous against a scan loop that is a nanosleep on a
#: preemptible kernel, and finite so a wedged daemon fails in seconds.
TRIAL_DEADLINE_SECONDS = 10.0


@pytest.fixture
def api(rig):
    """The client from `conftest.py`, under the name this file has always used."""
    return rig


def a_daemon_with(native_device, tmp_path, **overrides):
    """A second daemon, configured differently. As a context manager.

    Several tests here are about what a daemon does *on startup* — a line map
    resolved against the board, a trace ring of two, no config to load — and
    none of those can be reached by talking to one that is already up.
    """
    harness = DaemonOnALoopbackPort(configuration_for(native_device, tmp_path, **overrides))
    harness.start()
    return harness


def store(rig, *graphs) -> None:
    for graph in graphs:
        rig.write_graph(graph["name"], json.dumps(graph))


def a_session_of(rig, *graphs):
    store(rig, *graphs)
    return rig.upload_graph_set([graph["name"] for graph in graphs])


def write_config(rig, name: str, *, line_map=None, graphs=(), **extra) -> None:
    rig.write_config(
        name,
        json.dumps(
            {
                "name": name,
                "line_map": BENCH_LINE_MAP if line_map is None else line_map,
                "graphs": list(graphs),
                **extra,
            }
        ),
    )


# ------------------------------------------------------------- the device ---


def test_the_daemon_connects_on_startup_and_says_what_it_found(api):
    device = api.read_device()
    assert device.connected
    assert device.board == "native"
    assert device.protocol_version == 1
    assert device.capacities is not None and device.capacities.max_graphs >= 1
    assert device.committed_set is None


def test_the_wiring_was_pushed_before_anything_else(api):
    # docs/developer/daemon.md §3.4: the compile-time safe levels are a
    # mitigation, and replacing them with this rig's is the daemon's first job
    # on connecting.
    assert api.read_device().has_wiring is True


def test_the_lines_are_reported_by_name_with_their_live_levels(api):
    lines = api.read_lines()
    assert [line.name for line in lines.input_lines] == ["start_switch", "lever"]
    valve = next(line for line in lines.output_lines if line.name == "reward_valve")
    # Its safe level is high and the board has just failed safe into it, which
    # is the concrete case the whole wiring move exists for.
    assert valve.safe_level_is_high is True
    assert valve.is_high_now is True


# ------------------------------------------------------------ the pin map ---
#
# docs/reference/protocol.md §3.6. The daemon asks the device which pins it has
# rather than keeping a copy of the firmware's table, and these are the three
# things that buys: labels that are the board's own word, a config that can
# name a pin instead of a bit position, and a disagreement that is refused
# loudly instead of driving the wrong line for a session.


def test_the_pin_labels_are_the_devices_own_word(api):
    lines = api.read_lines()
    assert lines.pin_labels_came_from == "device"
    # The native build has no pins and says so rather than borrowing a board's
    # labels -- a "D2" here would be exactly the lie this command prevents.
    assert lines.board_input_pins[:3] == ["sim0", "sim1", "sim2"]
    assert api.read_device().pin_labels_came_from == "device"


def test_a_line_may_name_a_pin_instead_of_a_number(native_device, tmp_path):
    """The point of the whole exercise: the config says the thing somebody can
    check against the hardware, and the daemon asks the board what it means."""
    daemon = a_daemon_with(
        native_device,
        tmp_path,
        line_map={
            "input_lines": [{"name": "lever", "pin_label": "sim6"}],
            "output_lines": [{"name": "valve", "pin_label": "sim3", "safe_level_is_high": True}],
        },
    )
    try:
        with daemon.client() as rig:
            rig.wait_until_ready(timeout_s=10)
            lines = rig.read_lines()
            assert [line.line_index for line in lines.input_lines] == [6]
            assert [line.line_index for line in lines.output_lines] == [3]
            # And it was pushed as that line: safe levels are a mask over
            # indices, so a resolution that had not happened would fail safe on
            # the wrong pin.
            assert rig.read_device().has_wiring is True
    finally:
        daemon.stop()


def test_a_pin_this_board_does_not_have_stops_the_daemon_connecting(native_device, tmp_path):
    """Loud, and early. The alternative is a rig that runs a whole session with
    a lever's line number driving a valve, which no test downstream can see."""
    daemon = a_daemon_with(
        native_device,
        tmp_path,
        line_map={"input_lines": [{"name": "lever", "pin_label": "D6"}], "output_lines": []},
    )
    try:
        with daemon.client() as rig:
            rig.wait_until_ready(timeout_s=10)
            device = rig.read_device()
            assert device.connected is False
            assert device.link is not None
            assert "D6" in device.link.last_error
            # And it says what this board does have, because "wrong" without
            # "and here is what is right" is a bug report rather than an error.
            assert "sim0" in device.link.last_error
    finally:
        daemon.stop()


def test_a_line_map_that_contradicts_the_board_is_refused_before_it_is_kept(api):
    """A write is not a place to discover this either. The rig keeps running on
    the map it had."""
    before = [line.name for line in api.read_lines().input_lines]
    contradiction = {
        "input_lines": [{"name": "start_switch", "line_index": 0, "pin_label": "sim5"}],
        "output_lines": [],
    }

    with pytest.raises(DaemonRefusedTheRequest) as refused:
        api.write_line_map("bench", json.dumps(contradiction))
    assert refused.value.error == "line_map_does_not_match_the_board"

    # Untouched: the names it had before are the names it has now.
    assert [line.name for line in api.read_lines().input_lines] == before


# ------------------------------------------------------- the serial monitor ---


def test_the_monitor_holds_both_directions_of_the_greeting(api):
    """The wire, in the protocol's own words.

    Opening the panel has to show what already happened -- the greeting, the
    wiring push -- because a monitor that started at "now" would miss every
    fault that had already occurred, which is most of them.
    """
    monitor = api.read_serial_monitor(limit=10)
    directions = [entry.direction for entry in monitor.entries]
    said = [entry.line for entry in monitor.entries]

    assert directions[0] == "to_device"
    assert '"msg_type":"hello"' in said[0]
    assert directions[1] == "from_device"
    assert '"msg_type":"hello_ack"' in said[1]
    # The whole line, CRC included: this is the transport's view, and a monitor
    # that showed only what the parser understood would hide the framing faults
    # somebody opens it for.
    assert '"crc"' in said[0]
    assert monitor.ring_capacity > 0
    assert monitor.lost_entries_before is None


def test_the_monitor_records_a_command_an_rpc_sent(api):
    """Not only what the background reader saw: every layer writes through the
    transport, so a command issued by an rpc shows up here too."""
    before = api.read_serial_monitor(limit=1).newest_entry_number
    api.read_lines()  # reads the device, so it sends a `state`

    after = api.read_serial_monitor(since_entry_number=before + 1, limit=50)
    said = " ".join(entry.line for entry in after.entries)
    assert '"msg_type":"state"' in said
    assert '"msg_type":"state_report"' in said


def test_a_cursor_the_ring_has_passed_is_told_so(api, daemon):
    """The same rule as the trace: a consumer that fell behind is told the
    range it lost rather than handed a shorter answer that looks complete."""
    assert api.read_serial_monitor(since_entry_number=0, limit=1).lost_entries_before is None

    # Nothing has been evicted yet, so force the question the other way: a
    # cursor before the oldest entry the ring still holds.
    monitor = daemon.service.line_monitor
    for index in range(monitor.ring_capacity + 10):
        monitor.record("to_device", f"line {index}")

    lost = api.read_serial_monitor(since_entry_number=0, limit=5)
    assert lost.lost_entries_before == lost.oldest_entry_number_still_held
    assert lost.oldest_entry_number_still_held > 0


# --------------------------------------------------------- the trial loop ---


def test_a_whole_trial_comes_back_named(api):
    a_session_of(api, timed_graph("go-nogo", 60))

    armed = api.configure_trial(193, graph="go-nogo", cap_milliseconds=5000)
    assert armed.graph_index == 0
    assert armed.elapsed_milliseconds >= 0

    api.start_trial(193)
    api.wait_for_trial(193, timeout_s=TRIAL_DEADLINE_SECONDS)

    result = api.read_trial_result()
    assert result.trial_id == 193
    assert result.outcome is TrialOutcome.HIT
    assert [visit.state_name for visit in result.visits] == ["Wait", "Done"]


def test_a_trial_naming_a_graph_outside_the_set_is_refused_with_the_set(api):
    store(api, timed_graph("catch", 60))
    a_session_of(api, timed_graph("go-nogo", 60))

    with pytest.raises(TheRigIsNotInAStateForThat) as refused:
        api.configure_trial(1, graph="catch")
    assert refused.value.error == "graph_not_in_set"
    assert "go-nogo" in refused.value.detail, "it says what the set has"


def test_a_trial_before_any_upload_is_refused(api):
    with pytest.raises(TheRigIsNotInAStateForThat) as refused:
        api.configure_trial(1, graph="go-nogo")
    assert refused.value.error == "no_graph_set"


def test_a_cancel_stops_a_running_trial(api):
    a_session_of(api, timed_graph("slow", 5000))
    api.configure_trial(44, graph="slow", cap_milliseconds=20000)
    api.start_trial(44)

    assert api.cancel_trial(44).cancelled is True
    api.wait_for_trial(44, timeout_s=TRIAL_DEADLINE_SECONDS)

    result = api.read_trial_result()
    assert result.outcome is TrialOutcome.CANCELLED
    assert result.cancel_reason is TrialCancelReason.HOST


def test_a_per_trial_patch_changes_a_duration_by_name(api):
    # By name, like everything else a caller says. The daemon knows which entry
    # of the shared pool that name became, because it built the set.
    from statemachined_client import DistributionPatch

    a_session_of(api, timed_graph("go-nogo", 500))
    api.configure_trial(
        5,
        graph="go-nogo",
        cap_milliseconds=5000,
        distribution_patches=[DistributionPatch("dwell", duration_ms=40)],
    )
    api.start_trial(5)
    api.wait_for_trial(5, timeout_s=TRIAL_DEADLINE_SECONDS)

    assert api.read_trial_result().visits[0].drawn_duration_ms == 40


# ------------------------------------------------------------- the trace ---


def test_the_trace_holds_everything_that_happened_in_order(api):
    a_session_of(api, timed_graph("go-nogo", 60))
    api.configure_trial(77, graph="go-nogo", cap_milliseconds=5000)
    api.start_trial(77)
    api.wait_for_trial(77, timeout_s=TRIAL_DEADLINE_SECONDS)

    entries = api.read_trace(limit=200).entries
    kinds = [entry.kind for entry in entries]
    # The device is not the only thing worth timestamping: aligning an external
    # signal to trial 77 needs to know when trial 77 was armed.
    assert "link_connected" in kinds
    assert "graph_set_uploaded" in kinds
    assert kinds.index("trial_configured") < kinds.index("trial_started")
    assert kinds.index("trial_started") < kinds.index(KIND_STATE_VISIT)
    assert kinds.index(KIND_STATE_VISIT) < kinds.index(KIND_TRIAL_RESULT)
    numbers = [entry.entry_number for entry in entries]
    assert numbers == sorted(numbers)


def test_a_visit_in_the_trace_carries_all_three_timebases(api):
    """The device's clock, its unwrapped form, and an estimate in host time.

    They cross in the entry's `payload` rather than as named fields, which is
    the one place this interface carries a dict on purpose: the set of fields
    differs per kind, and a message that named them all would name most of them
    absent most of the time. The four fields every kind has are named.
    """
    a_session_of(api, timed_graph("go-nogo", 60))
    # A heartbeat has gone out by now, so there is a clock estimate to place
    # the visit in host time.
    time.sleep(0.7)
    api.configure_trial(78, graph="go-nogo", cap_milliseconds=5000)
    api.start_trial(78)
    api.wait_for_trial(78, timeout_s=TRIAL_DEADLINE_SECONDS)

    visits = [
        entry for entry in api.read_trial_trace(78) if entry.kind == KIND_STATE_VISIT
    ]
    assert visits
    first = visits[0].payload
    assert first["state_name"] == "Wait"
    assert first["entered_device_microseconds"] > 0  # the evidence
    assert first["unwrapped_device_microseconds"] >= first["entered_device_microseconds"]
    assert first["entered_host_time"].endswith("Z")  # an estimate, labelled
    assert first["host_time_uncertainty_microseconds"] >= 0


def test_a_payload_number_arrives_as_an_int_and_not_a_float(api):
    """`google.protobuf.Struct` has one number type and it is a double.

    The seam turns an integral one back into an `int`, because the alternative
    is every consumer of a trace payload writing `int(payload[...])` and one of
    them forgetting. Worth asserting against a real daemon and not only at the
    seam: this is where the numbers are real.
    """
    a_session_of(api, timed_graph("go-nogo", 60))
    api.configure_trial(79, graph="go-nogo", cap_milliseconds=5000)
    api.start_trial(79)
    ended = api.wait_for_trial(79, timeout_s=TRIAL_DEADLINE_SECONDS)

    visits = [e for e in api.read_trial_trace(79) if e.kind == KIND_STATE_VISIT]
    assert isinstance(visits[0].payload["entered_device_microseconds"], int)
    assert isinstance(visits[0].payload["measured_duration_microseconds"], int)
    assert isinstance(ended.entry_number, int)


def test_the_trace_is_also_on_disk_as_it_arrives(api, tmp_path):
    a_session_of(api, timed_graph("go-nogo", 60))
    written = list((tmp_path / "trace").glob("trace-*.ndjson"))
    assert len(written) == 1
    kinds = [json.loads(line)["kind"] for line in written[0].read_text().splitlines()]
    assert "graph_set_uploaded" in kinds


def test_a_cursor_that_fell_out_of_the_ring_is_told_so(native_device, tmp_path):
    # The one place the ring's boundedness is visible from outside.
    daemon = a_daemon_with(native_device, tmp_path, trace_ring_entries=2)
    try:
        with daemon.client() as rig:
            rig.wait_until_ready(timeout_s=10)
            a_session_of(rig, timed_graph("go-nogo", 40))
            rig.configure_trial(1, graph="go-nogo", cap_milliseconds=5000)
            rig.start_trial(1)
            rig.wait_for_trial(1, timeout_s=TRIAL_DEADLINE_SECONDS)

            trace = rig.read_trace(since_entry_number=0)
            assert trace.ring_capacity == 2
            assert len(trace.entries) == 2  # not the six that happened
            assert trace.lost_entries_before is not None
    finally:
        daemon.stop()


# ------------------------------------------------------------- the streams ---


def test_the_state_stream_sends_a_frame_at_once(api):
    """**At once, and not on the first change.**

    A client that connected to a quiet rig and saw nothing would have no way to
    tell that from a rig that is not there.
    """
    with api.watch_state(timeout_s=10) as states:
        frame = next(iter(states))
    assert frame.connected is True
    assert frame.input_word is not None


def test_the_trace_stream_sends_every_entry(api):
    store(api, timed_graph("go-nogo", 40))
    with api.watch_trace(timeout_s=10) as entries:
        stream = iter(entries)
        api.upload_graph_set(["go-nogo"])
        kinds = []
        for entry in stream:
            kinds.append(entry.kind)
            if entry.kind == "graph_set_uploaded":
                break
    assert "graph_set_uploaded" in kinds


# ---------------------------------------------------------------- config ---


def test_the_rig_config_says_what_the_box_is_and_not_how_it_is_wired(api):
    """The split. The rig config is the box; the lines are not in it.

    A line map there would be a line map in `/etc/braemons`, which is a
    conffile the daemon would then have to write -- and a conffile the daemon
    writes is one that fights dpkg on every upgrade.
    """
    configuration = api.read_configuration()
    assert configuration.graph_mode == "set"
    assert configuration.startup_state_machine_config == "bench"
    assert not hasattr(configuration, "line_map")


def test_the_loaded_state_machine_config_is_what_names_the_lines(api):
    session = api.read_session()
    assert session.state_machine_config is not None
    assert session.state_machine_config.name == "bench"
    assert session.state_machine_config.still_in_the_store is True
    # Loaded is not open: the map is pushed, the graphs are not up, and nothing
    # will run until somebody says so.
    assert session.session_open is False
    assert [line.name for line in api.read_lines().input_lines] == ["start_switch", "lever"]


# ------------------------------------------------ the state-machine configs ---
#
# The half of the configuration a person owns: the line map and the graphs, in
# one self-contained file under /var/lib. These tests are against a real device
# because the interesting part is the boundary -- a config is only accepted
# once its map has been resolved against a board that answered `pins`.


def test_a_config_is_saved_without_touching_the_rig(api):
    """Saving is not loading. A UI that could only save by also arming the rig
    is a UI nobody edits during a session."""
    write_config(
        api,
        "second",
        description="another rig's wiring",
        line_map={
            "input_lines": [{"name": "paw", "pin_label": "sim2"}],
            "output_lines": [{"name": "tone", "pin_label": "sim7"}],
        },
        graphs=[timed_graph("blink", 5)],
    )

    listed = api.list_configs()
    assert listed.loaded == "bench"
    assert {config.name for config in listed.configs} == {"bench", "second"}
    # The rig is still wired the way it was: the save reached the disk, and
    # nothing else.
    assert [line.name for line in api.read_lines().input_lines] == ["start_switch", "lever"]


def test_loading_a_config_renames_the_lines_and_pushes_the_wiring(api):
    write_config(
        api,
        "second",
        line_map={
            "input_lines": [{"name": "paw", "pin_label": "sim2"}],
            "output_lines": [{"name": "tone", "pin_label": "sim7"}],
        },
    )
    assert api.load_config("second").wiring_pushed is True

    lines = api.read_lines()
    assert [line.name for line in lines.input_lines] == ["paw"]
    # Resolved from the pin by asking the board, which is the whole point of a
    # config naming pins: sim2 is line 2 because this device said so.
    assert [line.line_index for line in lines.input_lines] == [2]
    assert api.read_session().state_machine_config.name == "second"


def test_a_config_naming_a_pin_this_board_does_not_have_is_refused_at_load(api):
    """The case a self-contained config makes possible: one written for another
    rig, carried here, naming a hole this board does not have."""
    write_config(
        api,
        "elsewhere",
        board="uno_r4_minima",
        line_map={"input_lines": [{"name": "lever", "pin_label": "D6"}], "output_lines": []},
    )

    with pytest.raises(DaemonRefusedTheRequest) as refused:
        api.load_config("elsewhere")
    # **One word for one fact.** The routes said
    # `state_machine_config_does_not_match_the_board` here and
    # `line_map_does_not_match_the_board` on a line map write, for the same
    # exception — two names for "this board has not got that pin", chosen by
    # which URL you hit. `context` is `line_map`, which is what to change
    # either way.
    assert refused.value.error == "line_map_does_not_match_the_board"
    assert refused.value.context == "line_map"
    assert "D6" in refused.value.detail

    # Still running what it had, which is the difference between a refusal and
    # a rig that has been half-reconfigured.
    assert api.read_session().state_machine_config.name == "bench"
    assert [line.name for line in api.read_lines().input_lines] == ["start_switch", "lever"]


def test_a_line_map_edit_reaches_the_board_but_not_the_disk(api):
    """Where an edit in the Lines panel lands, and where it does not.

    It has to reach the board to be checked against the wire at all -- that is
    what the live dots are for -- and it must not reach the disk on every
    keystroke, or `revert` would mean nothing.
    """
    edited = {
        "input_lines": [{"name": "left_lever", "line_index": 0, "debounce_milliseconds": 12}],
        "output_lines": [{"name": "ready_lamp", "line_index": 0}],
    }
    result = api.write_line_map("bench", json.dumps(edited))

    assert result.pushed_to_device is True
    assert result.saved_to_the_store is False
    assert result.state_machine_config == "bench"

    # In the loaded config, in memory...
    assert [line.name for line in api.read_lines().input_lines] == ["left_lever"]
    # ...and not in the stored one, until somebody saves it.
    stored = json.loads(api.read_config("bench").text)
    assert [line["name"] for line in stored["line_map"]["input_lines"]] == [
        "start_switch",
        "lever",
    ]


# ------------------------------------------------------------- the session ---


def test_a_session_opens_from_the_loaded_config_and_closes(api):
    """What triald does at the top of a session, and what the bench button does.

    The same call, which is the point: a bench that exercised a different path
    would be a bench that proves nothing about the rig.
    """
    write_config(api, "loaded", graphs=[timed_graph("quick", 5)])
    api.load_config("loaded")

    opened = api.open_session()
    assert opened.slots == {"quick": 0}

    session = api.read_session()
    assert session.session_open is True
    assert session.committed_set.graph_names == ["quick"]

    # A trial runs without triald: the graphs are up, so it names one and goes.
    api.configure_trial(1, graph="quick")
    api.start_trial(1)
    api.wait_for_trial(1, timeout_s=TRIAL_DEADLINE_SECONDS)
    assert api.read_trial_result().trial_id == 1

    closed = api.close_session()
    assert closed.was_open is True
    assert closed.session.session_open is False
    # The board still holds its set, which is what makes a reconnect cheap.
    assert closed.session.committed_set.graph_names == ["quick"]


def test_closing_a_session_takes_an_armed_trial_away_and_says_which(api):
    """The one act closing performs on the board.

    An armed trial with nobody driving it is a rig that will run one more trial
    at whatever time somebody next touches a lever. Which trial was taken is a
    fact the caller needs: it armed it.
    """
    a_session_of(api, timed_graph("slow", 30_000))
    api.configure_trial(42, graph="slow", cap_milliseconds=60_000)

    closed = api.close_session()
    assert closed.was_open is True
    assert closed.cancelled_trial_id == 42


def test_closing_nothing_is_not_an_error_and_says_so(api):
    api.close_session()
    closed = api.close_session()
    assert closed.was_open is False
    assert closed.cancelled_trial_id is None


def test_opening_a_session_with_no_config_loaded_is_refused_by_name(native_device, tmp_path):
    daemon = a_daemon_with(native_device, tmp_path, startup_state_machine_config="")
    try:
        with daemon.client() as rig:
            rig.wait_until_ready(timeout_s=10)
            assert rig.read_session().state_machine_config is None

            with pytest.raises(TheRigIsNotInAStateForThat) as refused:
                rig.open_session()
            assert refused.value.error == "no_state_machine_config_loaded"
            # And it says what there is to load, because "nothing is loaded"
            # without "here is what you have" is a dead end at two in the
            # morning.
            assert "bench" in refused.value.detail
    finally:
        daemon.stop()


# ---------------------------------------------------- driving it by hand ---


def a_rig_loaded_with(rig, *graph_names, milliseconds=5):
    """Save a config, load it, open a session. What a bench does before a trial."""
    write_config(
        rig,
        "manual",
        graphs=[timed_graph(name, milliseconds) for name in graph_names],
    )
    rig.load_config("manual")
    return rig.open_session()


def test_a_trial_that_names_no_graph_gets_the_active_one(api):
    """The one call that makes a rig operable by a person rather than by triald.

    Without it every trial has to name a graph, which is right for triald --
    it names one per trial -- and wrong for somebody pressing a button, who
    chose the paradigm once when they sat down.
    """
    a_rig_loaded_with(api, "go", "nogo")
    assert api.read_session().active_graph == ""

    assert api.set_active_graph("nogo") == "nogo"
    assert api.read_session().active_graph == "nogo"

    armed = api.configure_trial(7)
    assert armed.graph == "nogo", "the trial named none, so it got the active one"


def test_a_named_graph_beats_the_active_one(api):
    """A default, not a mode. triald names a graph on every trial and must be
    unaffected by whatever somebody selected in a browser tab."""
    a_rig_loaded_with(api, "go", "nogo")
    api.set_active_graph("nogo")
    assert api.configure_trial(8, graph="go").graph == "go"


def test_a_trial_with_no_graph_and_no_selection_is_refused_by_name(api):
    a_rig_loaded_with(api, "go")
    with pytest.raises(TheRigIsNotInAStateForThat) as refused:
        api.configure_trial(9)
    assert refused.value.error == "no_graph_named"


def test_selecting_a_graph_the_board_is_not_holding_is_refused(api):
    """Caught at selection rather than at the moment somebody presses run,
    which is the difference between a refusal and a rig that looks armed."""
    a_rig_loaded_with(api, "go")
    with pytest.raises(DaemonRefusedTheRequest) as refused:
        api.set_active_graph("not-a-graph")
    assert refused.value.error == "graph_not_available"
    assert "go" in refused.value.detail, "it says what there is"


def test_switching_the_active_graph_pushes_nothing(api):
    """A set is uploaded once and a graph is switched by index, so this is cheap
    and safe mid-session -- which is the whole reason a session uploads a set."""
    opened = a_rig_loaded_with(api, "go", "nogo")
    api.set_active_graph("go")
    api.set_active_graph("nogo")
    assert api.read_session().committed_set.set_version == opened.set_version


# ----------------------------------------------------------- recordings ---


def test_a_recording_survives_the_ring_it_was_taken_from(native_device, tmp_path):
    """The reason a recording is a sink and not a poller of the trace.

    A ring of four evicts almost immediately; a recording that had polled would
    have missed whatever fell out between polls, and would not have known.
    """
    daemon = a_daemon_with(native_device, tmp_path, trace_ring_entries=4)
    try:
        with daemon.client() as rig:
            rig.wait_until_ready(timeout_s=10)
            rig.start_recording("longer-than-the-ring")
            for index in range(20):
                daemon.service.trace.append(KIND_STATE_VISIT, state_name=f"state-{index}")
            rig.stop_recording()

            kept = rig.read_recording_entries("longer-than-the-ring", limit=100)
            assert len(kept.entries) >= 20
            assert rig.read_trace().ring_capacity == 4
    finally:
        daemon.stop()


def test_a_second_recording_while_one_runs_is_refused(api):
    api.start_recording("first")
    with pytest.raises(DaemonRefusedTheRequest) as refused:
        api.start_recording("second")
    assert refused.value.error == "already_recording"
    assert "first" in refused.value.detail


def test_a_recording_is_on_disk_as_it_goes(api, daemon, tmp_path):
    api.start_recording("on-disk")
    daemon.service.trace.append(KIND_STATE_VISIT, state_name="mid-session")
    written = (tmp_path / "recordings" / "on-disk.ndjson").read_text()
    assert "mid-session" in written


# --------------------------------- watching a rig somebody else is driving ---


def test_the_session_is_open_when_triald_uploaded_the_set(api):
    """triald calls `Session/UploadGraphs`, not `Session/Open`.

    A web UI that only knew about the second would show "no session" beside a
    board running trials -- which is the one thing somebody watching over
    triald's shoulder must not be told.
    """
    a_session_of(api, timed_graph("driven", 5))

    session = api.read_session()
    assert session.session_open is True
    assert session.committed_set.graph_names == ["driven"]

    opened = [entry for entry in api.read_trace().entries if entry.kind == "session_opened"]
    assert opened and opened[-1].payload["opened_by"] == "graph_names", (
        "the trace says which of the two calls opened it"
    )


# --------------------------------------------------- the bench instrument ---

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def the_shipped_walk_started_without_a_button() -> dict:
    """`graphs/state-walk.json`, entered at Step1 instead of at Ready.

    The shipped graph waits for `start_switch`, which is a button on a bench
    and is not reachable from here -- the daemon sends commands to the device
    and never drives its inputs. So the trigger is the one thing stubbed out,
    by moving the entry past it. **Everything the walk is about is still the
    shipped file**: the six states, their order, the one 500 ms distribution
    they share, and the lamps they raise.
    """
    walk = json.loads((REPOSITORY_ROOT / "graphs" / "state-walk.json").read_text())
    walk["name"] = "state-walk-from-step-one"
    walk["entry"] = "Step1"
    # And `Ready` goes with it: a graph is refused if it carries a state the
    # entry cannot reach, which is the validator doing its job -- the trigger
    # state is unreachable the moment the entry moves past it.
    walk["states"] = [state for state in walk["states"] if state["name"] != "Ready"]
    return walk


def test_the_walk_marches_through_its_states_at_500_ms_each(api):
    """The graph exists so somebody can answer "is this rig doing anything",
    and this is the same question asked of the daemon.

    Six states in one order, each 500 ms, ending HIT. Measured by the device's
    own clock and reported in the result, which is the number worth asserting:
    the host's idea of when a state was entered carries an uncertainty and this
    does not.
    """
    # The walk names three lamps, and the fixture's map has one. So the map it
    # was authored against is loaded first -- by line number, since this board
    # has no D10 -- which is also the honest version of what a bench does: the
    # graph and the map that gives its names meaning arrive together.
    write_config(
        api,
        "walk",
        line_map={
            "input_lines": [{"name": "start_switch", "line_index": 0}],
            "output_lines": [
                {"name": "ready_lamp", "line_index": 0},
                {"name": "cue_lamp", "line_index": 1},
                {"name": "error_lamp", "line_index": 2},
            ],
        },
        graphs=[the_shipped_walk_started_without_a_button()],
    )
    api.load_config("walk")
    api.open_session()
    api.configure_trial(500, graph="state-walk-from-step-one")
    api.start_trial(500)
    api.wait_for_trial(500, timeout_s=15.0)

    result = api.read_trial_result()
    assert result.outcome is TrialOutcome.HIT

    visited = [visit.state_name for visit in result.visits]
    assert visited == ["Step1", "Step2", "Step3", "Step4", "Step5", "Step6", "Done"]

    # Drawn is what the distribution said; measured is what the device's clock
    # saw. Both are checked, because a graph that drew 500 and dwelt for 5
    # would pass a test that only read the draw.
    for visit in result.visits[:-1]:
        assert visit.drawn_duration_ms == 500
        measured_ms = visit.measured_duration_microseconds / 1000
        assert 495 <= measured_ms <= 520, f"{visit.state_name} dwelt {measured_ms} ms"


# --------------------------------------------------- the board on its own ---


def self_driving_graph(name: str, milliseconds: int, dwell_ms: int) -> dict:
    graph = timed_graph(name, milliseconds)
    graph["distributions"]["iti"] = {"kind": "fixed", "duration_ms": dwell_ms}
    graph["states"][1]["relight_after"] = "iti"
    return graph


def test_a_board_can_be_handed_the_job_of_arming_its_own_trials(api):
    a_session_of(api, self_driving_graph("shaping", 40, 20))

    handed_over = api.write_autorun(True, graph_name="shaping")
    assert handed_over.enabled is True
    assert handed_over.active is True

    # It runs trials nobody armed, and the daemon reads their results as trials
    # like any other -- which is what makes an unattended session recordable.
    first = wait_until(lambda: _last_result(api), timeout_seconds=10)
    assert first is not None and first.outcome is TrialOutcome.HIT
    assert wait_until(
        lambda: (_last_result(api) or first).trial_id > first.trial_id or None,
        timeout_seconds=10,
    )

    taken_back = api.write_autorun(False)
    assert taken_back.active is False
    assert api.read_autorun().active is False


def _last_result(rig):
    try:
        return rig.read_trial_result()
    except DaemonRefusedTheRequest:
        return None


def test_autorun_and_the_settings_it_is_saved_with_are_written_to_the_board(api):
    # `start_now` false is how a rig is set up: a save is refused on a board
    # that is running, and a board arming its own trials is never idle, so the
    # intent is recorded first and the power cycle is what acts on it.
    a_session_of(api, self_driving_graph("shaping", 40, 20))
    handed_over = api.write_autorun(True, graph_name="shaping", start_now=False)
    assert handed_over.enabled is True
    assert handed_over.active is False

    saved = api.save_settings()
    assert saved.has_set is True
    assert saved.autorun is True
    # Flash wear, made visible rather than left to be discovered.
    assert saved.written is True
    assert saved.write_count == 1

    # And saving again, unchanged, spends no erase cycle: the device compares
    # before it writes, so a button pressed twice costs the board nothing.
    again = api.save_settings()
    assert again.written is False
    assert again.write_count == 1

    api.write_autorun(False)


def test_handing_the_rig_over_is_written_down_beside_the_trials(api):
    # "Who armed trial 412" is a question the record has to answer: a run the
    # device armed itself looks otherwise identical to one the daemon armed.
    a_session_of(api, self_driving_graph("shaping", 40, 20))
    api.write_autorun(True, graph_name="shaping", start_now=False)
    api.save_settings()
    api.write_autorun(False)

    kinds = [entry.kind for entry in api.read_trace().entries]
    assert "autorun_changed" in kinds
    assert "settings_saved" in kinds


# ----------------------------------------------------------- the observers ---
#
# This daemon reports to nobody: it publishes to its trace and whoever wants it
# opens a stream. The list exists so a person can answer "is triald actually
# listening?", which is the first question when trials stop being recorded.


def test_nobody_is_watching_until_somebody_opens_a_stream(api):
    watching = api.read_observers()
    assert watching.count == 0
    assert watching.observers == []


def test_a_stream_appears_in_the_list_while_it_is_open_and_not_after(api):
    with api.watch_trace(observer="triald"):
        listed = wait_until(lambda: api.read_observers().observers or None)
        assert listed[0].name == "triald"
        assert listed[0].stream == "trace"

    # Cancelling the call is the whole of unsubscribing.
    assert wait_until(lambda: api.read_observers().count == 0 or None)


def test_an_observer_that_does_not_say_what_it_is_is_still_listed(api):
    # A browser tab is an observer too and has nothing useful to declare.
    with api.watch_state():
        listed = wait_until(lambda: api.read_observers().observers or None)
        assert listed[0].name == "unnamed"
        assert listed[0].stream == "state"


def test_the_list_says_how_much_an_observer_has_had(api):
    """The diagnostic that matters: connected and receiving nothing is a
    different fault from not connected."""
    a_session_of(api, timed_graph("watched", 20))

    with api.watch_trace(observer="triald", timeout_s=15) as entries:
        stream = iter(entries)
        api.configure_trial(1, graph="watched")
        api.start_trial(1)
        # Drain until the trial is over, so there is something to have counted.
        for entry in stream:
            if entry.kind == KIND_TRIAL_RESULT:
                break

        delivered = wait_until(
            lambda: next(
                (o.delivered for o in api.read_observers().observers if o.delivered), None
            )
        )
        assert delivered and delivered > 0


def test_two_observers_are_both_listed(api):
    with api.watch_trace(observer="triald"), api.watch_trace(observer="console"):
        names = wait_until(
            lambda: sorted(o.name for o in api.read_observers().observers)
            if api.read_observers().count == 2
            else None
        )
        assert names == ["console", "triald"]


def test_a_graph_that_is_not_in_the_store_is_refused_by_name(api):
    with pytest.raises(NoSuchDocument) as refused:
        api.upload_graph_set(["nonexistent"])
    assert refused.value.error == "no_such_graph"
