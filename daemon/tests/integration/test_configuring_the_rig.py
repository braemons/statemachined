# SPDX-License-Identifier: LGPL-3.0-or-later
"""Everything that is not the trial loop: setting a rig up, through the client.

The half of the API that exists for a person -- the board, the wiring, the
graph store, the two configurations, the recordings -- against a real daemon
holding a real device. The claim each test makes is that the round trip works:
what this client reads can be edited and handed back, and what it writes is
what the daemon then reports.

**The documents cross as text**, which is the change from the HTTP API. A
graph, a line map and a state-machine config are files somebody authored; the
daemon is the only thing that parses one. So "read it, change one field, send
it" is `json.loads`, a dict, and `json.dumps` -- and the trap that used to be
here, where a read added fields a write would then refuse, cannot exist.
"""

from __future__ import annotations

import json

import pytest
from rig_harness import timed_graph, wait_until
from statemachined_client import (
    DaemonRefusedTheRequest,
    NoSuchDocument,
    RigConfigurationPatch,
    TheRigIsNotInAStateForThat,
)

TRIAL_DEADLINE_SECONDS = 10.0


def store(rig, *graphs) -> None:
    for graph in graphs:
        rig.write_graph(graph["name"], json.dumps(graph))


def a_session_of(rig, *graphs):
    store(rig, *graphs)
    return rig.upload_graph_set([graph["name"] for graph in graphs])


def config_of(rig, name: str) -> dict:
    return json.loads(rig.read_config(name).text)


# ---------------------------------------------------------------- the board ---


def test_the_board_says_what_it_is_and_what_it_can_hold(rig):
    device = rig.read_device()

    assert device.connected
    assert device.board
    # Read from the board and never assumed: the reference board ships two
    # images and a Teensy is a different set of numbers again.
    assert device.capacities is not None
    assert device.capacities.max_states > 0
    assert device.scan is not None, "a board that quietly misses scans must be visible"


def test_health_and_state_answer_without_a_session(rig):
    health = rig.read_health()
    assert health.ok and health.device_connected

    state = rig.read_state()
    assert state.connected
    assert not state.running


# ----------------------------------------------------------------- the lines ---


def test_the_lines_read_back_are_the_ones_the_config_named(rig):
    lines = rig.read_lines()

    assert [line.name for line in lines.input_lines] == ["start_switch", "lever"]
    assert [line.name for line in lines.output_lines] == ["ready_lamp", "reward_valve"]
    # The only way anything outside the device can check that a graph's line
    # numbers reach the pins somebody wired.
    assert all(line.is_high_now is not None for line in lines.input_lines)
    assert lines.board_input_pins, "the board's own labels, indexed by line number"


def test_the_view_reports_the_debouncing_the_config_asked_for(rig):
    """It is part of the wiring somebody is looking at.

    Left out of the first cut of `LineMapView`, which made a rig's debouncing
    invisible to every reader -- including the panel whose whole job is to show
    what the wiring is.
    """
    a_line = _input_named(rig, "lever")
    assert a_line.debounce_milliseconds >= 0

    written = _with_the_input_line_changed(rig, "lever", debounce_milliseconds=12)
    rig.write_line_map("bench", json.dumps(written))

    assert _input_named(rig, "lever").debounce_milliseconds == 12


def test_a_line_renamed_in_the_config_is_renamed_on_the_rig(rig):
    """Renaming is free: names are the daemon's alone and never reach the wire.

    The **document** is what is written -- a line map is a file -- and the
    daemon pushes what the board needs out of it. The reply says which of those
    two things happened, because they come apart.
    """
    written = _with_the_input_line_changed(rig, "lever", name="lever_left")
    result = rig.write_line_map("bench", json.dumps(written))

    assert result.pushed_to_device
    assert [line.name for line in rig.read_lines().input_lines] == [
        "start_switch",
        "lever_left",
    ]


def test_a_line_map_the_board_cannot_have_is_refused_and_nothing_is_kept(rig):
    """Refused *before* anything is kept, so the rig carries on with what it had.

    That guarantee is the daemon's; what this says is that the refusal reaches
    a caller as something it can act on, and that the rig really is unchanged.
    """
    before = rig.read_lines()
    impossible = {
        "input_lines": [{"name": "impossible", "pin_label": "D999"}],
        "output_lines": [],
    }

    with pytest.raises(DaemonRefusedTheRequest) as refused:
        rig.write_line_map("bench", json.dumps(impossible))
    assert refused.value.context, "a refusal always names what to change"

    assert rig.read_lines().input_lines == before.input_lines


def _input_named(rig, name: str):
    return next(line for line in rig.read_lines().input_lines if line.name == name)


def _with_the_input_line_changed(rig, line_name: str, **changes) -> dict:
    """The stored line map, with one input line edited. As a document.

    This is the operation the API is shaped for, and it is now unremarkable:
    the map is text on the way out and text on the way back, so nothing a read
    added has to be stripped before a write.
    """
    config = config_of(rig, "bench")
    line_map = config["line_map"]
    for line in line_map["input_lines"]:
        if line["name"] == line_name:
            line.update(changes)
    return line_map


# --------------------------------------------------------------- the autorun ---


def test_a_board_can_be_told_to_arm_its_own_trials_without_starting(rig):
    """How a rig is actually set up: enable, save, power cycle.

    `start_now=False` is the whole reason that argument exists -- a save is
    refused on a board that is running, and a board arming its own trials is
    never idle. It was left out of the first cut of `WriteAutorunRequest`,
    which made this sequence unreachable over the API.
    """
    a_session_of(rig, timed_graph("shaping", outcome="HIT"))

    set_to = rig.write_autorun(
        True, graph_name="shaping", cap_milliseconds=5000, start_now=False
    )
    assert set_to.enabled

    autorun = rig.read_autorun()
    # `enabled` and `active` are not the same fact, and this is the case that
    # proves it: stored, and not driving trials right now.
    assert autorun.enabled
    assert not autorun.active

    rig.write_autorun(False)
    assert not rig.read_autorun().enabled


def test_enabling_autorun_with_no_graph_anywhere_is_refused_by_name(rig):
    with pytest.raises(DaemonRefusedTheRequest) as refused:
        rig.write_autorun(True)
    assert refused.value.error in {"no_graph_named", "not_ready", "bad_index"}


def test_saving_settings_to_the_board_reports_the_flash_wear(rig):
    """`write_count` is a budget made visible rather than a number to ignore.

    And a save that would store what is already stored costs no erase cycle,
    which is what a UI's save button depends on for not being harmful to press
    twice. This rpc answered `Ok` in the first cut of the interface, throwing
    both facts away.
    """
    a_session_of(rig, timed_graph("kept", outcome="HIT"))

    first = rig.save_settings()
    assert first.written
    assert first.write_count >= 1
    assert first.has_set and first.set_version >= 1

    again = rig.save_settings()
    assert not again.written, "the device compares before it writes"
    assert again.write_count == first.write_count


# ------------------------------------------------------------ the graph store ---


def test_a_graph_is_stored_read_back_and_listed(rig):
    store(rig, timed_graph("go-nogo", outcome="HIT"))

    assert json.loads(rig.read_graph("go-nogo").text)["entry"] == "Wait"
    listed = {graph.name: graph for graph in rig.list_graphs()}
    assert listed["go-nogo"].readable
    assert listed["go-nogo"].state_count == 2


def test_a_graph_that_could_not_run_never_reaches_the_store(rig):
    """Validated on the way in, so the store never holds a graph that cannot run.

    Which is what lets everything downstream treat a stored name as a real
    paradigm rather than as a name somebody typed.
    """
    with pytest.raises(DaemonRefusedTheRequest):
        rig.write_graph("broken", json.dumps({"name": "broken", "entry": "Nowhere", "states": []}))
    with pytest.raises(NoSuchDocument):
        rig.read_graph("broken")


def test_validating_a_graph_reports_the_room_that_is_left_pool_by_pool(rig):
    """The capacity half is the useful half, and it is six numbers.

    "You have room for two more graphs" is what somebody setting up a session
    wants to know. One total would say "it does not fit" without saying what to
    cut -- and a board's pools fill independently, so a set can be two states
    short of the limit with room for forty more transitions.
    """
    store(rig, timed_graph("go-nogo", outcome="HIT"))
    checked = rig.validate_graph("go-nogo")

    assert checked.valid
    assert checked.pool_usage is not None and checked.pool_capacity is not None
    assert checked.pool_usage.states == 2
    assert checked.pool_capacity.states >= checked.pool_usage.states
    assert checked.pool_capacity.transitions >= checked.pool_usage.transitions


def test_a_graph_in_the_committed_set_cannot_be_deleted(rig):
    a_session_of(rig, timed_graph("live", outcome="HIT"))

    with pytest.raises(DaemonRefusedTheRequest) as refused:
        rig.delete_graph("live")
    assert refused.value.error == "graph_in_use"


def test_previewing_one_graph_is_refused_while_a_sessions_set_is_committed(rig):
    """Losing a session's paradigms because somebody previewed a graph is not a
    recoverable mistake, and the device holds one set."""
    a_session_of(rig, timed_graph("a", outcome="HIT"), timed_graph("b", outcome="HIT"))

    with pytest.raises(DaemonRefusedTheRequest) as refused:
        rig.upload_graph("a")
    assert refused.value.error == "session_set_committed"


# -------------------------------------------------------------- the session ---


def test_a_session_reports_its_three_facts_separately(rig):
    """A config loaded, a session open, a set committed. Not one "ready" flag.

    The useful question is *which* of them is missing.
    """
    before = rig.read_session()
    assert before.state_machine_config is not None
    assert before.state_machine_config.name == "bench"
    assert before.committed_set is None

    a_session_of(rig, timed_graph("go-nogo", outcome="HIT"))

    after = rig.read_session()
    assert after.committed_set is not None
    assert after.committed_set.graph_names == ["go-nogo"]
    assert after.committed_set.pool_usage is not None, "six numbers, and not one"


def test_the_active_graph_is_a_default_that_an_explicit_name_beats(rig):
    """A default, not a mode.

    A person choosing a paradigm in a browser must not be able to change what a
    driven rig is running, which is why a trial that names a graph always wins.
    """
    graphs = [timed_graph("chosen", outcome="HIT"), timed_graph("named", outcome="HIT")]
    store(rig, *graphs)
    config = config_of(rig, "bench")
    config["graphs"] = graphs
    rig.write_config("bench", json.dumps(config))
    rig.load_config("bench")
    rig.open_session()

    assert rig.set_active_graph("chosen") == "chosen"
    assert rig.read_session().active_graph == "chosen"
    assert rig.configure_trial(1).graph == "chosen"
    assert rig.configure_trial(2, graph="named").graph == "named"

    assert rig.clear_active_graph() == ""
    with pytest.raises(TheRigIsNotInAStateForThat) as refused:
        rig.configure_trial(3)
    assert refused.value.error == "no_graph_named"


def test_closing_a_session_leaves_the_set_on_the_board(rig):
    # What makes a reconnect cheap. Unloading it would buy nothing but a slow
    # start next time.
    committed = a_session_of(rig, timed_graph("go-nogo", outcome="HIT"))
    closed = rig.close_session()

    assert closed.was_open is True
    assert closed.session is not None
    assert closed.session.committed_set is not None
    assert closed.session.committed_set.set_version == committed.set_version


# ------------------------------------------------------ the two configurations ---


def test_a_state_machine_config_is_saved_without_being_loaded(rig):
    """Saving and loading are different verbs, and this is why it matters.

    A UI that could only save by also arming the rig is a UI nobody edits
    during a session. The write answers with the whole store, and `loaded`
    beside it is what says the rig is still on the old one.
    """
    config = config_of(rig, "bench")
    config["name"] = "tuesday"
    config["description"] = "the box with the new lever"
    config["graphs"] = [timed_graph("go-nogo", outcome="HIT")]

    saved = rig.write_config("tuesday", json.dumps(config))
    assert saved.loaded == "bench", "saved, and the rig is still running what it had"
    assert "tuesday" in {summary.name for summary in saved.configs}

    loaded = rig.load_config("tuesday")
    assert loaded.loaded == "tuesday"
    assert loaded.wiring_pushed
    assert rig.list_configs().loaded == "tuesday"


def test_deleting_the_loaded_config_does_not_stop_the_rig(rig):
    """Deleting a file is not a request to stop an experiment.

    The rig goes on running what it was given and says the config is no longer
    in the store, which is the honest report of what happened.
    """
    rig.delete_config("bench")

    session = rig.read_session()
    assert session.state_machine_config is not None
    assert session.state_machine_config.name == "bench"
    assert session.state_machine_config.still_in_the_store is False


def test_the_rig_config_can_be_changed_and_says_it_will_not_survive(rig):
    """`/etc/braemons` is a conffile this daemon never writes.

    So a change lasts until the daemon restarts and then the file wins. Saying
    so is the honest behaviour for a conffile; a client that hid it would leave
    a person believing a change was permanent.
    """
    assert rig.read_configuration().startup_state_machine_config == "bench"

    changed = rig.patch_configuration(RigConfigurationPatch(expected_board=""))
    assert changed.until_restart
    assert rig.read_configuration().expected_board == ""


def test_the_session_seed_can_be_pinned_so_a_session_replays(rig):
    """The one setting somebody changes to reproduce a session.

    Left out of the first cut of the patch message, which made a reproduction
    something you could only set up by editing a file on the box.
    """
    rig.patch_configuration(RigConfigurationPatch(session_seed="0123"))
    assert rig.read_configuration().session_seed == "0123"

    # And an empty string is a value — "draw one per connection" — rather than
    # "leave it", which is what `optional` on the patch field is for.
    rig.patch_configuration(RigConfigurationPatch(session_seed=""))
    assert rig.read_configuration().session_seed == ""


def test_the_rig_config_will_not_change_under_an_armed_trial(rig):
    # A target that changed means a different device, and swapping the device
    # under a running trial would move the thing the trial is measured against.
    a_session_of(rig, timed_graph("slow", 30_000, outcome="HIT"))
    rig.configure_trial(1, graph="slow", cap_milliseconds=60_000)
    rig.start_trial(1)

    with pytest.raises(DaemonRefusedTheRequest) as refused:
        rig.patch_configuration(RigConfigurationPatch(session_seed="0123"))
    assert refused.value.error == "busy"

    rig.cancel_trial(1)


# ------------------------------------------------------------- the recordings ---


def test_a_recording_keeps_the_trials_that_ran_while_it_was_on(rig):
    a_session_of(rig, timed_graph("go-nogo", outcome="HIT"))

    rig.start_recording("tuesday-pilot", "the client's own test")
    rig.configure_trial(1, graph="go-nogo", cap_milliseconds=5000)
    rig.start_trial(1)
    rig.wait_for_trial(1, timeout_s=TRIAL_DEADLINE_SECONDS)
    stopped = rig.stop_recording()

    assert stopped.state == "stopped"
    manifest = rig.read_recording("tuesday-pilot")
    assert manifest.kind_counts["trial_result"] == 1
    entries = rig.read_recording_entries("tuesday-pilot").entries
    # The trace's own numbers, which is what makes a recording join back to the
    # trace and to a `.tdr` exactly rather than approximately.
    numbers = [entry.entry_number for entry in entries]
    assert numbers == sorted(numbers)


def test_a_pause_leaves_a_gap_that_the_manifest_states(rig):
    """Pausing does not blind the rig, and the jump in the numbers is where it was.

    A recording that renumbered its entries would be claiming it saw everything
    -- the one failure worse than not having recorded.
    """
    a_session_of(rig, timed_graph("go-nogo", outcome="HIT"))
    rig.start_recording("with-a-gap")

    def one_trial(trial_id: int) -> None:
        rig.configure_trial(trial_id, graph="go-nogo", cap_milliseconds=5000)
        rig.start_trial(trial_id)
        rig.wait_for_trial(trial_id, timeout_s=TRIAL_DEADLINE_SECONDS)

    one_trial(1)
    rig.pause_recording()
    one_trial(2)
    rig.resume_recording()
    one_trial(3)
    rig.stop_recording()

    manifest = rig.read_recording("with-a-gap")
    assert len(manifest.segments) == 2, "two stretches is one pause"
    trials = {
        entry.trial_id
        for entry in rig.read_recording_entries("with-a-gap", limit=1000).entries
    }
    assert 2 not in trials, "what happened during the pause is not in the file"
    assert {1, 3} <= trials


def test_clearing_is_not_stopping(rig):
    """"The last ten minutes were me testing a valve" is a different intention.

    A UI with only stop would make somebody delete a file to say it.
    """
    rig.start_recording("scratch")
    wait_until(lambda: rig.read_recording("scratch").entry_count > 0, timeout_seconds=3)

    rig.clear_recording()
    assert rig.read_recording("scratch").state == "recording"

    rig.stop_recording()
    with pytest.raises(DaemonRefusedTheRequest):
        rig.pause_recording()


def test_a_recording_being_written_cannot_be_deleted(rig):
    rig.start_recording("in-flight")
    with pytest.raises(DaemonRefusedTheRequest) as refused:
        rig.delete_recording("in-flight")
    assert refused.value.error == "recording_in_progress"

    rig.stop_recording()
    remaining = rig.delete_recording("in-flight")
    assert "in-flight" not in {each.name for each in remaining.recordings}


# -------------------------------------------------------------- the observers ---


def test_a_subscriber_shows_up_on_the_observer_list_by_name(rig):
    """A label, and the answer to the question that is otherwise a packet capture.

    When trials stop reaching a consumer: is nothing connected, or is something
    connected and receiving nothing?
    """
    assert rig.read_observers().count == 0

    with rig.watch_trace(observer="a-name-that-grants-nothing"):
        listed = wait_until(lambda: rig.read_observers().observers or None, timeout_seconds=5)
        assert listed[0].name == "a-name-that-grants-nothing"
        assert listed[0].stream == "trace"

    assert wait_until(lambda: rig.read_observers().count == 0 or None, timeout_seconds=5)


def test_the_serial_monitor_holds_what_actually_crossed_the_wire(rig):
    """For the moment the layers stop agreeing.

    The line map says the valve is line 3, the valve is not opening, and the
    question is what went down the wire. Nothing interprets anything here.
    """
    monitor = rig.read_serial_monitor(limit=10)

    assert monitor.ring_capacity > 0
    assert monitor.entries, "always recording, because the alternative is not"
    assert {"to_device", "from_device"} >= {entry.direction for entry in monitor.entries}

    everything = rig.read_serial_monitor(limit=5000)
    assert "hello" in "".join(entry.line for entry in everything.entries)
