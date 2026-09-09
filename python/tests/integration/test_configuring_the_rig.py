# SPDX-License-Identifier: LGPL-3.0-or-later
"""Everything that is not the trial loop: setting a rig up, through the client.

The half of the API that exists for a person -- the board, the wiring, the graph
store, the two configurations, the recordings -- against a real daemon holding a
real device. The claim each test makes is that the round trip works: what this
client reads can be edited and handed back, and what it writes is what the
daemon then reports.

That round trip is the thing a client gets wrong. Every one of these documents
is a pydantic model on the far end that forbids members it does not declare, so
"read it, change one field, send it" is either the obvious operation or a trap,
and there is no third possibility.
"""

from __future__ import annotations

import pytest
from rig_harness import timed_graph, wait_until
from statemachined.client import Conflict, NotFound

# ---------------------------------------------------------------- the board ---


def test_the_board_says_what_it_is_and_what_it_can_hold(rig):
    device = rig.device.describe()

    assert device["connected"] is True
    assert device["board"]
    # Read from the board and never assumed: the reference board ships two
    # images and a Teensy is a different set of numbers again.
    assert device["capabilities"]["max_states"] > 0
    assert device["scan"] is not None, "a board that quietly misses scans must be visible"


def test_health_and_state_answer_without_a_session(rig):
    assert rig.health() == {"ok": True, "device_connected": True}
    state = rig.state()
    assert state["connected"] is True
    assert state["trial_id"] is not None or state["running"] in (False, 0)


# ----------------------------------------------------------------- the lines ---


def test_the_lines_read_back_are_the_ones_the_config_named(rig):
    lines = rig.device.lines()

    assert [line["name"] for line in lines["input_lines"]] == ["start_switch", "lever"]
    assert [line["name"] for line in lines["output_lines"]] == ["ready_lamp", "reward_valve"]
    # The only way anything outside the device can check that a graph's line
    # numbers reach the pins somebody wired.
    assert all("is_high_now" in line for line in lines["input_lines"])
    assert lines["board_input_pins"], "the board's own labels, indexed by line number"


def test_a_line_read_edited_and_written_back_is_accepted(rig):
    """The operation the API was shaped for, and the one a client can break.

    `GET` adds `is_high_now` to each line and the board's pin lists beside them;
    `LineMap` forbids members it does not declare. If this client did not strip
    what the read added, every editor built on it would meet a 422 and its
    author would conclude the API is inconsistent.
    """
    lines = rig.device.lines()
    lines["input_lines"][1]["name"] = "lever_left"

    changed = rig.device.set_lines(lines)

    assert changed["pushed_to_device"] is True
    # Renaming is free: names are the daemon's alone and never reach the wire.
    assert [line["name"] for line in rig.device.lines()["input_lines"]] == [
        "start_switch",
        "lever_left",
    ]
    # And it is not on disk until somebody saves the config, which the reply says.
    assert changed["saved_to_the_store"] is False


def test_the_wiring_and_not_only_the_name_goes_to_the_board(rig):
    lines = rig.device.lines()
    lines["input_lines"][0]["debounce_milliseconds"] = 12
    lines["input_lines"][0]["reads_active_low"] = True

    rig.device.set_lines(lines)

    back = rig.device.lines()["input_lines"][0]
    assert back["debounce_milliseconds"] == 12
    assert back["reads_active_low"] is True


def test_a_line_map_the_board_cannot_have_is_refused_and_nothing_is_kept(rig):
    """Refused *before* anything is kept, so the rig carries on with what it had.

    That guarantee is the daemon's; what this test says is that the client
    surfaces it as a refusal a caller can act on rather than as a 422 nobody
    reads, and that the rig really is unchanged afterwards.
    """
    from statemachined.client import Invalid

    before = rig.device.lines()
    with pytest.raises(Invalid):
        rig.device.set_lines(
            {"input_lines": [{"name": "impossible", "pin_label": "D999"}], "output_lines": []}
        )

    assert rig.device.lines()["input_lines"] == before["input_lines"]


# --------------------------------------------------------------- the autorun ---


def test_a_board_can_be_told_to_arm_its_own_trials_without_starting(rig):
    """How a rig is actually set up: enable, save, power cycle.

    `start_now=False` is the whole reason that argument exists -- a save is
    refused on a board that is running, and a board arming its own trials is
    never idle.
    """
    rig.graphs.write(timed_graph("shaping", outcome="HIT"))
    rig.session.upload_graph_set(["shaping"])

    set_to = rig.device.set_autorun(
        True, graph_name="shaping", cap_milliseconds=5000, start_now=False
    )
    assert set_to["enabled"] is True

    autorun = rig.device.autorun()
    # `enabled` and `active` are not the same fact, and this is the case that
    # proves it: stored, and not driving trials right now.
    assert autorun["enabled"] is True
    assert autorun.get("active") in (False, 0, None)

    rig.device.set_autorun(False)
    assert rig.device.autorun()["enabled"] is False


def test_enabling_autorun_with_no_graph_anywhere_is_refused_by_name(rig):
    with pytest.raises(Conflict) as refused:
        rig.device.set_autorun(True)
    assert refused.value.error in {"no_graph_named", "not_ready", "bad_index"}


def test_saving_settings_to_the_board_reports_the_flash_wear(rig):
    """`write_count` is a budget made visible rather than a number to ignore.

    And a save that would store what is already stored costs no erase cycle,
    which is the behaviour a UI's save button depends on for not being harmful
    to press twice.
    """
    rig.graphs.write(timed_graph("kept", outcome="HIT"))
    rig.session.upload_graph_set(["kept"])

    first = rig.device.save()
    assert first["written"] is True
    assert first["write_count"] >= 1

    again = rig.device.save()
    assert again["written"] is False, "the device compares before it writes"
    assert again["write_count"] == first["write_count"]


# ------------------------------------------------------------ the graph store ---


def test_a_graph_is_stored_read_back_and_listed(rig):
    rig.graphs.write(timed_graph("go-nogo", outcome="HIT"))

    assert rig.graphs.read("go-nogo")["entry"] == "Wait"
    listed = {graph["name"]: graph for graph in rig.graphs.list()}
    assert listed["go-nogo"]["readable"] is True
    assert listed["go-nogo"]["state_count"] == 2


def test_a_graph_that_could_not_run_never_reaches_the_store(rig):
    """Validated on the way in, so the store never holds a graph that cannot run.

    Which is what lets everything downstream treat a stored name as a real
    paradigm rather than as a name somebody typed.
    """
    from statemachined.client import Invalid

    with pytest.raises(Invalid):
        rig.graphs.write({"name": "broken", "entry": "Nowhere", "states": []})
    with pytest.raises(NotFound):
        rig.graphs.read("broken")


def test_validating_a_graph_reports_the_room_that_is_left(rig):
    """The capacity half is the useful half.

    "You have room for two more graphs" is what somebody setting up a session
    wants to know, and it is why usage is reported rather than only checked.
    """
    rig.graphs.write(timed_graph("go-nogo", outcome="HIT"))
    checked = rig.graphs.validate("go-nogo")

    assert checked["valid"] is True
    assert checked["pool_usage"]["states"] == 2
    assert checked["pool_capacity"]["states"] >= checked["pool_usage"]["states"]


def test_a_graph_in_the_committed_set_cannot_be_deleted(rig):
    rig.graphs.write(timed_graph("live", outcome="HIT"))
    rig.session.upload_graph_set(["live"])

    with pytest.raises(Conflict) as refused:
        rig.graphs.delete("live")
    assert refused.value.error == "graph_in_use"


def test_previewing_one_graph_is_refused_while_a_sessions_set_is_committed(rig):
    """Losing a session's paradigms because somebody previewed a graph is not a
    recoverable mistake, and the device holds one set."""
    for name in ("a", "b"):
        rig.graphs.write(timed_graph(name, outcome="HIT"))
    rig.session.upload_graph_set(["a", "b"])

    with pytest.raises(Conflict) as refused:
        rig.graphs.upload("a")
    assert refused.value.error == "session_set_committed"


# -------------------------------------------------------------- the session ---


def test_a_session_reports_its_three_facts_separately(rig):
    """A config loaded, a session open, a set committed. Not one "ready" flag.

    The useful question is *which* of them is missing.
    """
    before = rig.session.read()
    assert before["state_machine_config"]["name"] == "bench"
    assert before["committed_set"] is None

    rig.graphs.write(timed_graph("go-nogo", outcome="HIT"))
    rig.session.upload_graph_set(["go-nogo"])

    after = rig.session.read()
    assert after["committed_set"]["graph_names"] == ["go-nogo"]


def test_the_active_graph_is_a_default_that_an_explicit_name_beats(rig):
    """A default, not a mode.

    A person choosing a paradigm in a browser must not be able to change what a
    driven rig is running, which is why a trial that names a graph always wins.
    """
    for name in ("chosen", "named"):
        rig.graphs.write(timed_graph(name, outcome="HIT"))
    config = rig.configs.read("bench")
    config["graphs"] = [rig.graphs.read("chosen"), rig.graphs.read("named")]
    rig.configs.write(config)
    rig.configs.load("bench")
    rig.session.open()

    rig.session.set_active_graph("chosen")
    assert rig.session.read()["active_graph"] == "chosen"
    assert rig.trial.configure(1)["graph"] == "chosen"
    assert rig.trial.configure(2, graph="named")["graph"] == "named"

    rig.session.clear_active_graph()
    with pytest.raises(Conflict) as refused:
        rig.trial.configure(3)
    assert refused.value.error == "no_graph_named"


def test_closing_a_session_leaves_the_set_on_the_board(rig):
    # What makes a reconnect cheap. Unloading it would buy nothing but a slow
    # start next time.
    rig.graphs.write(timed_graph("go-nogo", outcome="HIT"))
    committed = rig.session.upload_graph_set(["go-nogo"])
    rig.session.close()

    assert rig.session.read()["committed_set"]["set_version"] == committed["set_version"]


# ------------------------------------------------------ the two configurations ---


def test_a_state_machine_config_is_saved_without_being_loaded(rig):
    """Saving and loading are different verbs, and this is why it matters.

    A UI that could only save by also arming the rig is a UI nobody edits during
    a session.
    """
    config = rig.configs.read("bench")
    config["name"] = "tuesday"
    config["description"] = "the box with the new lever"
    config["graphs"] = [timed_graph("go-nogo", outcome="HIT")]

    saved = rig.configs.write(config)
    assert saved["is_the_loaded_config"] is False
    assert rig.configs.list()["loaded"] == "bench"

    loaded = rig.configs.load("tuesday")
    assert loaded["loaded"] == "tuesday"
    assert loaded["wiring_pushed"] is True
    assert rig.configs.list()["loaded"] == "tuesday"


def test_deleting_the_loaded_config_does_not_stop_the_rig(rig):
    """Deleting a file is not a request to stop an experiment.

    The rig goes on running what it was given and says the config is no longer
    in the store, which is the honest report of what happened.
    """
    rig.configs.delete("bench")

    session = rig.session.read()
    assert session["state_machine_config"]["name"] == "bench"
    assert session["state_machine_config"]["is_still_in_the_store"] is False


def test_the_rig_config_can_be_changed_and_says_it_will_not_survive(rig):
    """`/etc/braemons` is a conffile this daemon never writes.

    So a change lasts until the daemon restarts and then the file wins. Saying
    so is the honest behaviour for a conffile; a client that hid it would leave
    a person believing a change was permanent.
    """
    assert rig.rig.read()["startup_state_machine_config"] == "bench"

    changed = rig.rig.update(expected_board="")
    assert changed["until_restart"] is True
    assert rig.rig.read()["expected_board"] == ""


def test_the_rig_config_will_not_change_under_an_armed_trial(rig):
    # A target that changed means a different device, and swapping the device
    # under a running trial would move the thing the trial is measured against.
    rig.graphs.write(timed_graph("slow", 30_000, outcome="HIT"))
    rig.session.upload_graph_set(["slow"])
    rig.trial.configure(1, graph="slow", cap_milliseconds=60_000)
    rig.trial.start(1)

    with pytest.raises(Conflict) as refused:
        rig.rig.update(session_seed="0123")
    assert refused.value.error == "busy"

    rig.trial.cancel(1)


# ------------------------------------------------------------- the recordings ---


def test_a_recording_keeps_the_trials_that_ran_while_it_was_on(rig):
    rig.graphs.write(timed_graph("go-nogo", outcome="HIT"))
    rig.session.upload_graph_set(["go-nogo"])

    rig.recordings.start("tuesday-pilot", description="the client's own test")
    with rig.trace.subscribe() as stream:
        rig.trial.configure(1, graph="go-nogo", cap_milliseconds=5000)
        rig.trial.start(1)
        assert stream.wait_for_trial(1, timeout_seconds=10)
    stopped = rig.recordings.stop()

    assert stopped["state"] == "stopped"
    manifest = rig.recordings.read("tuesday-pilot")
    assert manifest["kind_counts"]["trial_result"] == 1
    entries = rig.recordings.entries("tuesday-pilot")["entries"]
    # The trace's own numbers, which is what makes a recording join back to the
    # trace and to a `.tdr` exactly rather than approximately.
    assert [entry["entry_number"] for entry in entries] == sorted(
        entry["entry_number"] for entry in entries
    )


def test_a_pause_leaves_a_gap_that_the_manifest_states(rig):
    """Pausing does not blind the rig, and the jump in the numbers is where it was.

    A recording that renumbered its entries would be claiming it saw everything
    -- the one failure worse than not having recorded.
    """
    rig.graphs.write(timed_graph("go-nogo", outcome="HIT"))
    rig.session.upload_graph_set(["go-nogo"])
    rig.recordings.start("with-a-gap")

    def one_trial(trial_id: int) -> None:
        with rig.trace.subscribe() as stream:
            rig.trial.configure(trial_id, graph="go-nogo", cap_milliseconds=5000)
            rig.trial.start(trial_id)
            assert stream.wait_for_trial(trial_id, timeout_seconds=10)

    one_trial(1)
    rig.recordings.pause()
    one_trial(2)
    rig.recordings.resume()
    one_trial(3)
    rig.recordings.stop()

    manifest = rig.recordings.read("with-a-gap")
    assert len(manifest["segments"]) == 2, "two stretches is one pause"
    trials = {
        entry.get("trial_id")
        for entry in rig.recordings.entries("with-a-gap", limit=1000)["entries"]
    }
    assert 2 not in trials, "what happened during the pause is not in the file"
    assert {1, 3} <= trials


def test_clearing_is_not_stopping(rig):
    """"The last ten minutes were me testing a valve" is a different intention.

    A UI with only stop would make somebody delete a file to say it.
    """
    rig.recordings.start("scratch")
    wait_until(lambda: rig.recordings.read("scratch")["entry_count"] > 0, timeout_seconds=3)

    rig.recordings.clear()
    assert rig.recordings.read("scratch")["state"] == "recording"

    rig.recordings.stop()
    with pytest.raises(Conflict):
        rig.recordings.pause()


def test_a_recording_being_written_cannot_be_deleted(rig):
    rig.recordings.start("in-flight")
    with pytest.raises(Conflict) as refused:
        rig.recordings.delete("in-flight")
    assert refused.value.error == "recording_in_progress"

    rig.recordings.stop()
    assert rig.recordings.delete("in-flight")["deleted"] == "in-flight"


# -------------------------------------------------------------- the observers ---


def test_a_subscriber_shows_up_on_the_observer_list_by_name(rig):
    """A label, and the answer to the question that is otherwise a packet capture.

    When trials stop reaching a consumer: is nothing connected, or is something
    connected and receiving nothing?
    """
    assert rig.trace.observers()["count"] == 0

    with rig.trace.subscribe("a-name-that-grants-nothing"):
        listed = wait_until(lambda: rig.trace.observers()["observers"], timeout_seconds=5)
        assert listed and listed[0]["name"] == "a-name-that-grants-nothing"
        assert listed[0]["stream"] == "trace"

    assert wait_until(lambda: rig.trace.observers()["count"] == 0, timeout_seconds=5) is not None


def test_the_line_monitor_holds_what_actually_crossed_the_wire(rig):
    """For the moment the layers stop agreeing.

    The line map says the valve is line 3, the valve is not opening, and the
    question is what went down the wire. Nothing interprets anything here.
    """
    monitor = rig.device.monitor(limit=10)

    assert monitor["ring_capacity"] > 0
    assert monitor["lines"], "always recording, because the alternative is not"
    assert {"to_device", "from_device"} >= {line["direction"] for line in monitor["lines"]}
    assert "hello" in "".join(line["line"] for line in rig.device.monitor(limit=5000)["lines"])
