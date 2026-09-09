# SPDX-License-Identifier: LGPL-3.0-or-later
"""Which path, which body, which timeout: the client's own half of every call.

Each test here is a claim about the wire that the daemon cannot make on this
client's behalf. `docs/reference/api.md` is the source for every path and field name, and
where a test looks pedantic -- asserting that `configure` sends `graph` and not
`statemachine_graph`, say -- it is because that field is renamed at exactly one
boundary and this is the boundary.
"""

from __future__ import annotations

import pytest
from statemachined.client import UPLOAD_TIMEOUT_SECONDS

# ------------------------------------------------------------ the trial loop ---


def test_configure_names_the_graph_and_the_trial(client, rig):
    rig.will_answer({"trial_id": 193, "graph": "go-nogo", "set_version": 8})

    armed = client.trial.configure(193, graph="go-nogo", cap_milliseconds=30_000)

    assert rig.call.method == "POST"
    assert rig.call.path == "/api/trial/configure"
    # `graph`, and never a slot index: the daemon built the set, so it is the
    # only process that knows which slot a name became.
    assert rig.call.body == {
        "trial_id": 193,
        "graph": "go-nogo",
        "cap_milliseconds": 30_000,
        "start_source": "serial",
    }
    assert armed["set_version"] == 8


def test_configure_may_name_no_graph_at_all(client, rig):
    # The bench caller: a person who chose their paradigm once when they sat
    # down. The daemon fills it in from the active graph.
    client.trial.configure(1)
    assert rig.call.body["graph"] == ""


def test_distribution_patches_go_out_only_when_there_are_some(client, rig):
    # An empty list is not the same as no field to a validator that forbids
    # extras, and the daemon's default is the empty list either way; sending
    # nothing keeps the arm as small as it can be, which is the point of it.
    client.trial.configure(1, graph="g")
    assert "distribution_patches" not in rig.call.body


def test_distribution_patches_are_passed_through_by_name(client, rig):
    client.trial.configure(
        1, graph="g", distribution_patches=[{"name": "foreperiod", "minimum_ms": 250}]
    )
    assert rig.call.body["distribution_patches"] == [{"name": "foreperiod", "minimum_ms": 250}]


def test_start_and_cancel_carry_the_trial_id_and_nothing_else(client, rig):
    rig.will_answer({"trial_id": 5}).will_answer({"trial_id": 5, "cancelled": True})

    client.trial.start(5)
    client.trial.cancel(5)

    assert [(call.path, call.body) for call in rig.calls] == [
        ("/api/trial/start", {"trial_id": 5}),
        ("/api/trial/cancel", {"trial_id": 5}),
    ]


def test_the_last_result_is_a_get_with_no_id(client, rig):
    # `result` is *the last one*. A trial by id is `trace.for_trial`, and the
    # difference matters: one is a convenience and the other is the recovery
    # path for a dropped notification.
    rig.will_answer({"trial_id": 5, "outcome": "HIT"})
    assert client.trial.result()["outcome"] == "HIT"
    assert (rig.call.method, rig.call.path) == ("GET", "/api/trial/result")


# --------------------------------------------------------------- the session ---


def test_uploading_a_graph_set_sends_the_names_in_order(client, rig):
    rig.will_answer({"set_version": 8, "slots": {"go-nogo": 0, "2afc": 1}})

    uploaded = client.session.upload_graph_set(["go-nogo", "2afc"])

    assert rig.call.path == "/api/session/graphs"
    # The order is the slot assignment, so a client that sorted or de-duplicated
    # would silently renumber a session's paradigms.
    assert rig.call.body == {"graph_names": ["go-nogo", "2afc"]}
    assert uploaded["slots"]["2afc"] == 1


def test_the_uploads_get_the_long_timeout_and_the_trial_loop_does_not(client, rig):
    """The slowest call in the API must not be cut off by the default deadline.

    Tens of seconds on a UART rig, and it is the call that exists to let a
    session fail *before* an animal is in the booth -- a ten-second timeout
    would turn it into the thing that fails.
    """
    client.session.upload_graph_set(["a"])
    client.session.open()
    client.graphs.upload("a")
    client.trial.configure(1, graph="a")

    slow = [call.timeout_seconds for call in rig.calls[:3]]
    assert slow == [UPLOAD_TIMEOUT_SECONDS] * 3
    assert rig.calls[3].timeout_seconds == client._transport.timeout_seconds


def test_selecting_and_clearing_the_active_graph(client, rig):
    rig.will_answer({"active_graph": "go-nogo"}).will_answer({"active_graph": None})

    client.session.set_active_graph("go-nogo")
    client.session.clear_active_graph()

    assert [(call.method, call.path) for call in rig.calls] == [
        ("PUT", "/api/session/active-graph"),
        ("DELETE", "/api/session/active-graph"),
    ]
    assert rig.calls[0].body == {"graph": "go-nogo"}


# ---------------------------------------------------------------- the graphs ---


def test_listing_graphs_unwraps_the_envelope(client, rig):
    rig.will_answer({"graphs": [{"name": "go-nogo", "readable": True, "state_count": 4}]})
    assert client.graphs.list() == [{"name": "go-nogo", "readable": True, "state_count": 4}]


def test_a_graph_is_stored_under_its_own_name(client, rig):
    # The daemon refuses a mismatch, and it is a refusal nobody should have to
    # meet: the graph says what it is called.
    client.graphs.write({"name": "go-nogo", "entry": "Wait", "states": []})
    assert rig.call.path == "/api/graphs/go-nogo"
    assert rig.call.method == "PUT"


def test_a_graph_may_be_stored_under_a_name_given_explicitly(client, rig):
    client.graphs.write({"name": "go-nogo", "states": []}, name="other")
    assert rig.call.path == "/api/graphs/other"


def test_validate_and_upload_are_posts_under_the_graph(client, rig):
    rig.will_answer({"valid": True}).will_answer({"set_version": 1})
    client.graphs.validate("go-nogo")
    client.graphs.upload("go-nogo")
    assert [call.path for call in rig.calls] == [
        "/api/graphs/go-nogo/validate",
        "/api/graphs/go-nogo/upload",
    ]


# ---------------------------------------------------------------- the device ---


def test_autorun_leaves_out_what_it_was_not_told(client, rig):
    """A field left out means "whatever the board holds stands".

    Which is not the same as sending a zero: the seed a board restored from its
    own storage is holding is the thing that makes an unattended session replay,
    and a client that sent `seed: 0` on every call would silently reset it.
    """
    client.device.set_autorun(True, graph_name="shaping", start_now=False)

    assert rig.call.method == "PUT"
    assert rig.call.body == {
        "enabled": True,
        "cap_milliseconds": 0,
        "start_now": False,
        "graph_name": "shaping",
    }
    assert "seed" not in rig.call.body
    assert "first_trial_id" not in rig.call.body


def test_autorun_sends_the_seed_and_first_trial_when_it_is_given_them(client, rig):
    client.device.set_autorun(True, seed=81985529216486895, first_trial_id=1)
    assert rig.call.body["seed"] == 81985529216486895
    assert rig.call.body["first_trial_id"] == 1


def test_saving_settings_sends_no_body(client, rig):
    """What is saved is what is there.

    A save that took its own copy of the settings would be a second place for
    them to disagree, so the route takes nothing -- and a client that helpfully
    sent the current settings would be inventing that second place.
    """
    client.device.save()
    assert rig.call.path == "/api/device/save"
    assert rig.call.body is None


def test_the_line_map_read_back_can_be_written_straight_out_again(client, rig):
    """Read, edit, write. The one shape the API was designed for.

    `GET /api/device/lines` adds `is_high_now` to each line and the board's own
    pin lists beside them; `LineMap` forbids members it does not declare. So a
    client that handed the answer back unchanged would be refused 422, and a
    person would conclude the API is inconsistent rather than that the client
    is.
    """
    rig.will_answer(
        {
            "input_lines": [{"name": "lever", "line_index": 4, "is_high_now": True}],
            "output_lines": [{"name": "valve", "line_index": 3, "is_high_now": False}],
            "pin_labels_came_from": "device",
            "board_input_pins": ["D2", "D3"],
            "board_output_pins": ["D10"],
        }
    ).will_answer({"pushed_to_device": True})

    lines = client.device.lines()
    lines["input_lines"][0]["name"] = "lever_left"
    client.device.set_lines(lines)

    assert rig.calls[1].method == "PATCH"
    assert rig.calls[1].body == {
        "input_lines": [{"name": "lever_left", "line_index": 4}],
        "output_lines": [{"name": "valve", "line_index": 3}],
    }


def test_a_field_the_client_does_not_know_is_still_sent(client, rig):
    """Stripping is confined to the four names the API documents.

    A client that filtered a line down to the members it had heard of would
    silently drop a field added to `LineMap` after it was written -- and the
    caller would never learn, because the daemon would accept the smaller map.
    A genuine typo must still be refused by the daemon, naming the field.
    """
    client.device.set_lines(
        {"input_lines": [{"name": "lever", "line_index": 4, "invented_field": 1}]}
    )
    assert rig.call.body["input_lines"][0]["invented_field"] == 1


def test_the_monitor_pages_by_cursor(client, rig):
    client.device.monitor(since_entry_number=120, limit=50)
    assert rig.call.query == {"since_entry_number": "120", "limit": "50"}


# ----------------------------------------------------------------- the trace ---


def test_the_trace_for_one_trial_unwraps_its_entries(client, rig):
    rig.will_answer({"trial_id": 7, "entries": [{"kind": "visit"}, {"kind": "trial_result"}]})
    assert client.trace.for_trial(7) == [{"kind": "visit"}, {"kind": "trial_result"}]
    assert rig.call.path == "/api/trace/trial/7"


def test_reading_the_trace_keeps_the_whole_envelope(client, rig):
    """Unlike a trial's entries, and on purpose.

    The cursor fields are the answer here: `lost_entries_before` is the only
    place the ring's boundedness is visible from outside, and a client that
    returned the entries alone would throw away the one thing that says the
    answer is short.
    """
    rig.will_answer(
        {"entries": [], "newest_entry_number": 9, "lost_entries_before": 4, "ring_capacity": 100}
    )
    read = client.trace.read(since_entry_number=2)
    assert read["lost_entries_before"] == 4
    assert rig.call.query == {"since_entry_number": "2", "limit": "500"}


# --------------------------------------------------------- the configurations ---


def test_updating_the_rig_config_merges_onto_what_is_there(client, rig):
    """`PATCH /api/config` takes a whole configuration, so this reads first.

    The daemon validates it as one object -- a partial body would be refused for
    the fields it left out -- so a convenience that sent only the change would
    be a convenience that never worked.
    """
    rig.will_answer(
        {"device_target": "/dev/ttyACM0", "expected_board": "uno_r4_minima", "device_baud": 115200}
    ).will_answer({"reconnected": True, "until_restart": True})

    client.rig.update(device_target="socket://127.0.0.1:5300")

    assert rig.calls[0].method == "GET"
    assert rig.calls[1].method == "PATCH"
    assert rig.calls[1].body == {
        "device_target": "socket://127.0.0.1:5300",
        "expected_board": "uno_r4_minima",
        "device_baud": 115200,
    }


def test_a_state_machine_config_is_saved_under_its_own_name(client, rig):
    client.configs.write({"name": "bench", "line_map": {}, "graphs": []})
    assert (rig.call.method, rig.call.path) == ("PUT", "/api/state-machine-configs/bench")


def test_loading_a_config_is_a_different_verb_from_saving_one(client, rig):
    # Saving touches no hardware; loading resolves the map against the board and
    # pushes the wiring. A client that collapsed them would make the UI arm the
    # rig on every keystroke.
    rig.will_answer({"loaded": "bench"})
    client.configs.load("bench")
    assert (rig.call.method, rig.call.path) == (
        "POST",
        "/api/state-machine-configs/bench/load",
    )


# ------------------------------------------------------------ the recordings ---


@pytest.mark.parametrize(
    ("verb", "path"),
    [
        ("start", "/api/recordings/start"),
        ("pause", "/api/recordings/pause"),
        ("resume", "/api/recordings/resume"),
        ("stop", "/api/recordings/stop"),
        ("clear", "/api/recordings/clear"),
    ],
)
def test_each_recording_verb_is_its_own_route(client, rig, verb, path):
    # Four intentions, four verbs, and `clear` is the one that is not a stop.
    getattr(client.recordings, verb)()
    assert (rig.call.method, rig.call.path) == ("POST", path)


def test_a_recording_starts_with_a_name_it_may_not_have(client, rig):
    client.recordings.start()
    assert rig.call.body == {"name": "", "description": ""}


def test_recording_entries_are_read_by_position(client, rig):
    # By position and not by entry number: a recording with a pause in it has no
    # contiguous range of entry numbers, because the numbers are the trace's.
    client.recordings.entries("tuesday-pilot", offset=200, limit=50)
    assert rig.call.path == "/api/recordings/tuesday-pilot/entries"
    assert rig.call.query == {"offset": "200", "limit": "50"}


# ------------------------------------------------------------------ the rest ---


def test_health_asks_two_questions_and_reports_both(client, rig):
    rig.will_answer({"ok": True, "device_connected": False})
    # A daemon whose board is unplugged is still the thing you ask why, so this
    # is an answer rather than a refusal.
    assert client.health() == {"ok": True, "device_connected": False}


def test_an_empty_answer_is_a_dict_and_not_none(client, rig):
    """`DELETE` answers with nothing, and a caller should not have to know which.

    The alternative is every call site branching on None for the handful of
    routes that happen to be empty.
    """
    rig.will_answer_with_text("", status_code=200)
    assert client.graphs.delete("gone") == {}
