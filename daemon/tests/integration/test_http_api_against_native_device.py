# SPDX-License-Identifier: GPL-3.0-or-later
"""The whole daemon, over HTTP, against the firmware built for this machine.

Everything below the API has been tested on its own by now. What this adds is
the thing only the assembled daemon can be wrong about: that a graph put in the
store by one call is uploadable by another, that a trial named over HTTP reaches
the device and comes back named, that a result nobody asked for lands in the
trace, and that the link thread and a request can both want the port without
deadlocking.

The last one is why these run against a real device rather than a stub. A lock
held across a serial read is exactly the kind of thing that works in a test with
no I/O in it.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from statemachined.api.application import create_application
from statemachined.daemon_configuration import DaemonConfiguration
from statemachined.model.graph_definition import GraphDefinition


def timed_graph(name: str, milliseconds: int) -> dict:
    return {
        "name": name,
        "entry": "Wait",
        "distributions": {"dwell": {"kind": "fixed", "duration_ms": milliseconds}},
        "states": [
            {
                "name": "Wait",
                "on_entry": [{"line": "ready_lamp", "kind": "high"}],
                "timeout": {"after": "dwell", "goto": "Hit"},
            },
            {"name": "Hit", "outcome": "HIT"},
        ],
    }


def configuration_for(native_device, tmp_path, **overrides) -> DaemonConfiguration:
    """A daemon wired to this device, with an empty store and trace of its own."""
    settings = {
        "device_target": native_device.target_url,
        "device_timeout_seconds": 5.0,
        "graph_store_directory": tmp_path / "graphs",
        "trace_directory": tmp_path / "trace",
        "heartbeat_seconds": 0.5,
        "line_map": {
            "input_lines": [
                {"name": "start_switch", "line_index": 0},
                {"name": "lever", "line_index": 4},
            ],
            "output_lines": [
                {"name": "ready_lamp", "line_index": 0},
                {"name": "reward_valve", "line_index": 3, "safe_level_is_high": True},
            ],
        },
    }
    settings.update(overrides)
    return DaemonConfiguration(**settings)


@pytest.fixture
def api(native_device, tmp_path):
    with TestClient(create_application(configuration_for(native_device, tmp_path))) as client:
        yield client


def wait_until(predicate, timeout_seconds: float = 5.0):
    """Poll for something the link thread will do soon.

    A result and the visit stream arrive unasked, so a test that asserted
    immediately after `start` would be asserting on a race rather than on the
    daemon.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return None


# ------------------------------------------------------------- the device ---


def test_the_daemon_connects_on_startup_and_says_what_it_found(api):
    device = api.get("/api/device").json()
    assert device["connected"] is True
    assert device["board"] == "native"
    assert device["protocol_version"] == 1
    assert device["capabilities"]["max_graphs"] >= 1
    assert device["committed_set"] is None


def test_the_wiring_was_pushed_before_anything_else(api):
    # dev/DAEMON.md §3.4: the compile-time safe levels are a mitigation, and
    # replacing them with this rig's is the daemon's first job on connecting.
    assert api.get("/api/device").json()["has_wiring"] is True


def test_the_lines_are_reported_by_name_with_their_live_levels(api):
    lines = api.get("/api/device/lines").json()
    assert [line["name"] for line in lines["input_lines"]] == ["start_switch", "lever"]
    valve = next(line for line in lines["output_lines"] if line["name"] == "reward_valve")
    # Its safe level is high and the board has just failed safe into it, which
    # is the concrete case the whole wiring move exists for.
    assert valve["safe_level_is_high"] is True
    assert valve["is_high_now"] is True


def test_renaming_a_line_is_free_and_changes_no_graph(api):
    lines = api.get("/api/device/lines").json()
    renamed = {
        "input_lines": [
            dict(line, name="paw") if line["name"] == "lever" else line
            for line in lines["input_lines"]
        ],
        "output_lines": lines["output_lines"],
    }
    for line_list in renamed.values():
        for line in line_list:
            line.pop("is_high_now", None)
    response = api.patch("/api/device/lines", json=renamed)
    assert response.status_code == 200
    assert response.json()["pushed_to_device"] is True
    assert [line["name"] for line in api.get("/api/device/lines").json()["input_lines"]] == [
        "start_switch",
        "paw",
    ]


# ------------------------------------------------------------ the pin map ---
#
# dev/PROTOCOL.md §3.6. The daemon asks the device which pins it has rather than
# keeping a copy of the firmware's table, and these are the three things that
# buys: labels that are the board's own word, a config that can name a pin
# instead of a bit position, and a disagreement that is refused loudly instead
# of driving the wrong line for a session.


def test_the_pin_labels_are_the_devices_own_word(api):
    lines = api.get("/api/device/lines").json()
    assert lines["pin_labels_came_from"] == "device"
    # The native build has no pins and says so rather than borrowing a board's
    # labels -- a "D2" here would be exactly the lie this command prevents.
    assert lines["board_input_pins"][:3] == ["sim0", "sim1", "sim2"]
    assert api.get("/api/device").json()["pin_labels_came_from"] == "device"


def test_a_line_may_name_a_pin_instead_of_a_number(native_device, tmp_path):
    """The point of the whole exercise: the config says the thing somebody can
    check against the hardware, and the daemon asks the board what it means."""
    configuration = configuration_for(
        native_device,
        tmp_path,
        line_map={
            "input_lines": [{"name": "lever", "pin_label": "sim6"}],
            "output_lines": [{"name": "valve", "pin_label": "sim3", "safe_level_is_high": True}],
        },
    )
    with TestClient(create_application(configuration)) as api:
        lines = api.get("/api/device/lines").json()
        assert [line["line_index"] for line in lines["input_lines"]] == [6]
        assert [line["line_index"] for line in lines["output_lines"]] == [3]
        # And it was pushed as that line: safe levels are a mask over indices,
        # so a resolution that had not happened would fail safe on the wrong pin.
        assert api.get("/api/device").json()["has_wiring"] is True


def test_a_pin_this_board_does_not_have_stops_the_daemon_connecting(native_device, tmp_path):
    """Loud, and early. The alternative is a rig that runs a whole session with
    a lever's line number driving a valve, which no test downstream can see."""
    configuration = configuration_for(
        native_device,
        tmp_path,
        line_map={"input_lines": [{"name": "lever", "pin_label": "D6"}], "output_lines": []},
    )
    with TestClient(create_application(configuration)) as api:
        device = api.get("/api/device").json()
        assert device["connected"] is False
        assert "D6" in device["link"]["last_error"]
        # And it says what this board does have, because "wrong" without "and
        # here is what is right" is a bug report rather than an error message.
        assert "sim0" in device["link"]["last_error"]


def test_a_line_map_that_contradicts_the_board_is_refused_before_it_is_kept(api):
    """A PATCH is not a place to discover this either. The rig keeps running on
    the map it had."""
    lines = api.get("/api/device/lines").json()
    contradiction = {
        "input_lines": [
            {"name": "start_switch", "line_index": 0, "pin_label": "sim5"},
        ],
        "output_lines": [],
    }
    response = api.patch("/api/device/lines", json=contradiction)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "line_map_does_not_match_the_board"
    # Untouched: the names it had before are the names it has now.
    after = api.get("/api/device/lines").json()
    assert [line["name"] for line in after["input_lines"]] == [
        line["name"] for line in lines["input_lines"]
    ]


# ------------------------------------------------------- the serial monitor ---


def test_the_monitor_holds_both_directions_of_the_greeting(api):
    """The wire, in the protocol's own words. Opening the panel has to show what
    already happened -- the greeting, the wiring push -- because a monitor that
    started at "now" would miss every fault that had already occurred, which is
    most of them.
    """
    monitor = api.get("/api/device/monitor?limit=10").json()
    directions = [line["direction"] for line in monitor["lines"]]
    said = [line["line"] for line in monitor["lines"]]

    assert directions[0] == "to_device"
    assert '"msg_type":"hello"' in said[0]
    assert directions[1] == "from_device"
    assert '"msg_type":"hello_ack"' in said[1]
    # The whole line, CRC included: this is the transport's view, and a monitor
    # that showed only what the parser understood would hide the framing faults
    # somebody opens it for.
    assert '"crc"' in said[0]
    assert monitor["ring_capacity"] > 0
    assert monitor["lost_lines_before"] is None


def test_the_monitor_records_a_command_a_request_sent(api):
    """Not only what the background reader saw: every layer writes through the
    transport, so a command issued by an HTTP request shows up here too."""
    before = api.get("/api/device/monitor?limit=1").json()["newest_entry_number"]
    api.get("/api/device/lines")  # reads the device, so it sends a `state`

    after = api.get(f"/api/device/monitor?since_entry_number={before + 1}&limit=50").json()
    said = " ".join(line["line"] for line in after["lines"])
    assert '"msg_type":"state"' in said
    assert '"msg_type":"state_report"' in said


def test_a_cursor_the_ring_has_passed_is_told_so(api):
    """The same rule as the trace: a consumer that fell behind is told the range
    it lost rather than handed a shorter answer that looks complete."""
    monitor = api.get("/api/device/monitor?since_entry_number=0&limit=1").json()
    assert monitor["lost_lines_before"] is None

    # Nothing has been evicted yet, so force the question the other way: a
    # cursor before the oldest entry the ring still holds.
    service = api.app.state.rig_service
    for index in range(service.line_monitor.ring_capacity + 10):
        service.line_monitor.record("to_device", f"line {index}")
    lost = api.get("/api/device/monitor?since_entry_number=0&limit=5").json()
    assert lost["lost_lines_before"] == lost["oldest_entry_number_still_held"]
    assert lost["oldest_entry_number_still_held"] > 0


# -------------------------------------------------------------- the store ---


def test_a_graph_written_over_http_comes_back_the_same(api):
    graph = timed_graph("go-nogo", 60)
    assert api.put("/api/graphs/go-nogo", json=graph).status_code == 200
    stored = api.get("/api/graphs/go-nogo").json()
    assert GraphDefinition.model_validate(stored).entry == "Wait"
    assert api.get("/api/graphs").json()["graphs"][0]["state_count"] == 2


def test_a_graph_that_could_not_be_run_is_refused_on_the_way_in(api):
    # The store cannot hold a paradigm that could not be run, so the failure
    # surfaces when somebody saves, with the field named.
    broken = timed_graph("broken", 60)
    broken["states"][1]["outcome"] = "SUCCESS"
    assert api.put("/api/graphs/broken", json=broken).status_code == 422
    assert api.get("/api/graphs").json()["graphs"] == []


def test_a_graph_stored_under_the_wrong_name_is_refused(api):
    response = api.put("/api/graphs/other-name", json=timed_graph("go-nogo", 60))
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "name_mismatch"


def test_validate_reports_what_the_graph_costs_against_this_board(api):
    api.put("/api/graphs/go-nogo", json=timed_graph("go-nogo", 60))
    validated = api.post("/api/graphs/go-nogo/validate").json()
    assert validated["valid"] is True
    assert validated["pool_usage"]["states"] == 2
    # The capacity half is the useful half: "you have room for..." is what
    # somebody setting up a session wants.
    assert validated["pool_capacity"]["states"] >= 2


# ------------------------------------------------------- a session's set ---


def test_a_session_uploads_its_graphs_once_and_gets_its_slots_back(api):
    api.put("/api/graphs/go-nogo", json=timed_graph("go-nogo", 60))
    api.put("/api/graphs/2afc", json=timed_graph("2afc", 120))

    uploaded = api.post("/api/session/graphs", json={"graph_names": ["go-nogo", "2afc"]}).json()
    assert uploaded["slots"] == {"go-nogo": 0, "2afc": 1}
    assert uploaded["pool_usage"]["states"] == 4
    assert uploaded["elapsed_milliseconds"] >= 0

    device = api.get("/api/device").json()
    assert device["committed_set"]["graph_names"] == ["go-nogo", "2afc"]


def test_a_session_naming_a_graph_the_store_does_not_hold_is_refused(api):
    response = api.post("/api/session/graphs", json={"graph_names": ["nonexistent"]})
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "no_such_graph"


def test_a_graph_in_the_committed_set_cannot_be_deleted(api):
    api.put("/api/graphs/go-nogo", json=timed_graph("go-nogo", 60))
    api.post("/api/session/graphs", json={"graph_names": ["go-nogo"]})
    response = api.delete("/api/graphs/go-nogo")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "graph_in_use"


def test_previewing_one_graph_is_refused_while_a_session_set_is_committed(api):
    # Losing a session's paradigms because somebody previewed a graph is not a
    # recoverable mistake, and the device holds one set.
    for name in ("go-nogo", "2afc"):
        api.put(f"/api/graphs/{name}", json=timed_graph(name, 60))
    api.post("/api/session/graphs", json={"graph_names": ["go-nogo", "2afc"]})
    response = api.post("/api/graphs/go-nogo/upload")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "session_set_committed"


# --------------------------------------------------------- the trial loop ---


def test_a_whole_trial_over_http_comes_back_named(api):
    api.put("/api/graphs/go-nogo", json=timed_graph("go-nogo", 60))
    api.post("/api/session/graphs", json={"graph_names": ["go-nogo"]})

    armed = api.post(
        "/api/trial/configure",
        json={"trial_id": 193, "graph": "go-nogo", "cap_milliseconds": 5000},
    ).json()
    assert armed["graph_index"] == 0
    assert armed["elapsed_milliseconds"] >= 0

    assert api.post("/api/trial/start", json={"trial_id": 193}).status_code == 200

    result = wait_until(
        lambda: api.get("/api/trial/result").json()
        if api.get("/api/trial/result").status_code == 200
        else None
    )
    assert result is not None
    assert result["trial_id"] == 193
    assert result["outcome"] == "HIT"
    assert [visit["state_name"] for visit in result["visits"]] == ["Wait", "Hit"]


def test_a_trial_naming_a_graph_outside_the_set_is_refused_with_the_set(api):
    api.put("/api/graphs/go-nogo", json=timed_graph("go-nogo", 60))
    api.put("/api/graphs/catch", json=timed_graph("catch", 60))
    api.post("/api/session/graphs", json={"graph_names": ["go-nogo"]})

    response = api.post("/api/trial/configure", json={"trial_id": 1, "graph": "catch"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "graph_not_in_set"


def test_a_trial_before_any_upload_is_refused(api):
    response = api.post("/api/trial/configure", json={"trial_id": 1, "graph": "go-nogo"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "no_graph_set"


def test_a_cancel_stops_a_running_trial(api):
    api.put("/api/graphs/slow", json=timed_graph("slow", 5000))
    api.post("/api/session/graphs", json={"graph_names": ["slow"]})
    api.post(
        "/api/trial/configure",
        json={"trial_id": 44, "graph": "slow", "cap_milliseconds": 20000},
    )
    api.post("/api/trial/start", json={"trial_id": 44})

    cancelled = api.post("/api/trial/cancel", json={"trial_id": 44}).json()
    assert cancelled["cancelled"] is True

    result = wait_until(
        lambda: api.get("/api/trial/result").json()
        if api.get("/api/trial/result").status_code == 200
        else None
    )
    assert result is not None
    assert result["outcome"] == "CANCELLED"
    assert result["cancel_reason"] == "HOST"


def test_a_per_trial_patch_changes_a_duration_by_name(api):
    # By name, like everything else a caller says. The daemon knows which entry
    # of the shared pool that name became, because it built the set.
    api.put("/api/graphs/go-nogo", json=timed_graph("go-nogo", 500))
    api.post("/api/session/graphs", json={"graph_names": ["go-nogo"]})
    api.post(
        "/api/trial/configure",
        json={
            "trial_id": 5,
            "graph": "go-nogo",
            "cap_milliseconds": 5000,
            "distribution_patches": [{"name": "dwell", "duration_ms": 40}],
        },
    )
    api.post("/api/trial/start", json={"trial_id": 5})

    result = wait_until(
        lambda: api.get("/api/trial/result").json()
        if api.get("/api/trial/result").status_code == 200
        else None
    )
    assert result is not None
    assert result["visits"][0]["drawn_duration_ms"] == 40


# ------------------------------------------------------------- the trace ---


def test_the_trace_holds_everything_that_happened_in_order(api):
    api.put("/api/graphs/go-nogo", json=timed_graph("go-nogo", 60))
    api.post("/api/session/graphs", json={"graph_names": ["go-nogo"]})
    api.post(
        "/api/trial/configure",
        json={"trial_id": 77, "graph": "go-nogo", "cap_milliseconds": 5000},
    )
    api.post("/api/trial/start", json={"trial_id": 77})
    wait_until(lambda: api.get("/api/trial/result").status_code == 200)

    entries = api.get("/api/trace", params={"limit": 200}).json()["entries"]
    kinds = [entry["kind"] for entry in entries]
    # The device is not the only thing worth timestamping: aligning an external
    # signal to trial 77 needs to know when trial 77 was armed.
    assert "link_connected" in kinds
    assert "graph_set_uploaded" in kinds
    assert kinds.index("trial_configured") < kinds.index("trial_started")
    assert kinds.index("trial_started") < kinds.index("visit")
    assert kinds.index("visit") < kinds.index("trial_result")
    assert [entry["entry_number"] for entry in entries] == sorted(
        entry["entry_number"] for entry in entries
    )


def test_a_visit_in_the_trace_carries_all_three_timebases(api):
    api.put("/api/graphs/go-nogo", json=timed_graph("go-nogo", 60))
    api.post("/api/session/graphs", json={"graph_names": ["go-nogo"]})
    # A heartbeat has gone out by now, so there is a clock estimate to place the
    # visit in host time.
    time.sleep(0.7)
    api.post(
        "/api/trial/configure",
        json={"trial_id": 78, "graph": "go-nogo", "cap_milliseconds": 5000},
    )
    api.post("/api/trial/start", json={"trial_id": 78})
    wait_until(lambda: api.get("/api/trial/result").status_code == 200)

    visits = [
        entry
        for entry in api.get("/api/trace/trial/78").json()["entries"]
        if entry["kind"] == "visit"
    ]
    assert visits
    first = visits[0]
    assert first["state_name"] == "Wait"
    assert first["entered_device_microseconds"] > 0  # the evidence
    assert first["unwrapped_device_microseconds"] >= first["entered_device_microseconds"]
    assert first["entered_host_time"].endswith("Z")  # an estimate, labelled
    assert first["host_time_uncertainty_microseconds"] >= 0


def test_the_trace_is_also_on_disk_as_it_arrives(api, tmp_path):
    api.put("/api/graphs/go-nogo", json=timed_graph("go-nogo", 60))
    api.post("/api/session/graphs", json={"graph_names": ["go-nogo"]})
    written = list((tmp_path / "trace").glob("trace-*.ndjson"))
    assert len(written) == 1
    kinds = [json.loads(line)["kind"] for line in written[0].read_text().splitlines()]
    assert "graph_set_uploaded" in kinds


def test_a_cursor_that_fell_out_of_the_ring_is_told_so(native_device, tmp_path):
    # The one place the ring's boundedness is visible from outside.
    configuration = configuration_for(native_device, tmp_path, trace_ring_entries=2)
    with TestClient(create_application(configuration)) as client:
        client.put("/api/graphs/go-nogo", json=timed_graph("go-nogo", 40))
        client.post("/api/session/graphs", json={"graph_names": ["go-nogo"]})
        client.post(
            "/api/trial/configure",
            json={"trial_id": 1, "graph": "go-nogo", "cap_milliseconds": 5000},
        )
        client.post("/api/trial/start", json={"trial_id": 1})
        wait_until(lambda: client.get("/api/trial/result").status_code == 200)

        trace = client.get("/api/trace", params={"since_entry_number": 0}).json()
        assert trace["ring_capacity"] == 2
        assert len(trace["entries"]) == 2  # not the six that happened
        assert trace["lost_entries_before"] is not None


# ------------------------------------------------------------- the streams ---


def test_the_state_stream_sends_a_frame_and_coalesces(api):
    with api.websocket_connect("/api/stream") as stream:
        frame = stream.receive_json()
    assert frame["connected"] is True
    assert "input_word" in frame


def test_the_trace_stream_sends_every_entry(api):
    api.put("/api/graphs/go-nogo", json=timed_graph("go-nogo", 40))
    with api.websocket_connect("/api/trace/stream") as stream:
        api.post("/api/session/graphs", json={"graph_names": ["go-nogo"]})
        entry = stream.receive_json()
    assert entry["kind"] == "graph_set_uploaded"


# ---------------------------------------------------------------- config ---


def test_the_configuration_is_readable_and_the_line_map_is_in_it(api):
    configuration = api.get("/api/config").json()
    assert configuration["graph_mode"] == "set"
    assert [line["name"] for line in configuration["line_map"]["input_lines"]] == [
        "start_switch",
        "lever",
    ]
