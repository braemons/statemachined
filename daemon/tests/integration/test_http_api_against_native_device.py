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
