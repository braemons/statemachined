# SPDX-License-Identifier: GPL-3.0-or-later
"""The ring, the cursor, and the one boundary a caller has to be told about."""

from __future__ import annotations

import json

from statemachined.device.state_visit_trace import KIND_STATE_VISIT, StateVisitTrace


def test_entries_are_numbered_by_the_daemon_not_by_the_device():
    # The device's `seq` counts visits within a run and restarts at zero every
    # trial, so it cannot address a position in a log that spans a session --
    # which is what a `since_` cursor needs.
    trace = StateVisitTrace(ring_entries=10)
    first = trace.append(KIND_STATE_VISIT, trial_id=1, device_sequence_number=0)
    second = trace.append(KIND_STATE_VISIT, trial_id=2, device_sequence_number=0)
    assert first["entry_number"] == 0
    assert second["entry_number"] == 1
    assert second["device_sequence_number"] == 0


def test_a_cursor_returns_what_came_after_it():
    trace = StateVisitTrace(ring_entries=10)
    for index in range(5):
        trace.append(KIND_STATE_VISIT, trial_id=index)
    assert [entry["trial_id"] for entry in trace.entries_since(3)] == [3, 4]


def test_the_ring_drops_the_oldest_and_says_which():
    trace = StateVisitTrace(ring_entries=3)
    for index in range(5):
        trace.append(KIND_STATE_VISIT, trial_id=index)
    assert len(trace) == 3
    assert trace.oldest_entry_number_still_held() == 2
    assert trace.newest_entry_number() == 4


def test_a_cursor_that_has_fallen_out_of_the_ring_is_detectable():
    # The one place the ring's boundedness is visible from outside. A consumer
    # slow enough for this has genuinely lost data and must be told, rather than
    # handed a shorter answer that looks complete.
    trace = StateVisitTrace(ring_entries=3)
    for index in range(5):
        trace.append(KIND_STATE_VISIT, trial_id=index)
    assert trace.has_fallen_out_of_the_ring(0)
    assert not trace.has_fallen_out_of_the_ring(2)


def test_one_trial_can_be_asked_for_on_its_own():
    trace = StateVisitTrace(ring_entries=10)
    trace.append(KIND_STATE_VISIT, trial_id=1)
    trace.append(KIND_STATE_VISIT, trial_id=2)
    trace.append(KIND_STATE_VISIT, trial_id=1)
    assert len(trace.entries_for_trial(1)) == 2


def test_every_entry_is_written_to_the_file_as_it_arrives(tmp_path):
    # On arrival and not on eviction, which is the whole difference: a crash
    # otherwise loses exactly the window that mattered most -- everything still
    # in the ring.
    trace = StateVisitTrace(ring_entries=2, directory=tmp_path)
    for index in range(5):
        trace.append(KIND_STATE_VISIT, trial_id=index)

    written = list(tmp_path.glob("trace-*.ndjson"))
    assert len(written) == 1
    lines = written[0].read_text().splitlines()
    assert len(lines) == 5  # all five, though the ring only ever held two
    assert [json.loads(line)["trial_id"] for line in lines] == [0, 1, 2, 3, 4]


def test_a_disk_that_cannot_be_written_does_not_stop_a_session(tmp_path):
    # The ring is what the API reads, so the trace survives this. Losing a
    # session over a full disk would be worse than losing the durable copy.
    unwritable = tmp_path / "file-in-the-way"
    unwritable.write_text("not a directory")
    trace = StateVisitTrace(ring_entries=10, directory=unwritable / "trace")
    trace.append(KIND_STATE_VISIT, trial_id=1)
    assert len(trace) == 1


def test_every_entry_carries_the_host_time_it_was_recorded_at():
    trace = StateVisitTrace(ring_entries=10)
    entry = trace.append(KIND_STATE_VISIT, trial_id=1)
    assert entry["recorded_host_time"].endswith("Z")
    assert "T" in entry["recorded_host_time"]
