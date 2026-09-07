# SPDX-License-Identifier: GPL-3.0-or-later
"""A recording keeps what somebody asked for, and admits what it did not.

The trace is always on and bounded; a recording is a name, a boundary and a file
that is only this run. These tests are about the difference, and the one that
matters most is the pause: a paused recording is not a blinded rig, it is a
**gap**, and a recording that closed over its own gap would be worse than not
recording at all.
"""

from __future__ import annotations

import json

import pytest
from statemachined.device.state_visit_trace import StateVisitTrace
from statemachined.event_recording import (
    BadRecordingName,
    EventRecorder,
    RecordingNameTaken,
    RecordingNotInStore,
    RecordingStateRefused,
)


@pytest.fixture
def recorder(tmp_path):
    return EventRecorder(tmp_path / "recordings")


def a_trace_feeding(recorder: EventRecorder) -> StateVisitTrace:
    trace = StateVisitTrace(1000)
    trace.add_sink(recorder.record)
    return trace


def test_nothing_is_captured_until_somebody_starts_one(recorder) -> None:
    trace = a_trace_feeding(recorder)
    trace.append("visit", state_name="waiting")
    assert recorder.active is None
    assert recorder.manifests() == []


def test_what_happens_while_recording_is_in_the_file(recorder) -> None:
    trace = a_trace_feeding(recorder)
    recorder.start("morning")
    trace.append("visit", state_name="waiting")
    trace.append("visit", state_name="reward")
    recorder.stop()

    entries = recorder.entries_of("morning")
    assert [entry["state_name"] for entry in entries] == ["waiting", "reward"]
    assert recorder.manifest_of("morning")["entry_count"] == 2


def test_a_pause_leaves_a_gap_that_the_manifest_states(recorder) -> None:
    """The whole design, in one test.

    The trace keeps running through the pause -- that is the point, a rig is
    never blinded to make a recording tidy -- so the entries in the middle exist
    and are simply not in the recording. What must not happen is the recording
    looking continuous afterwards: the segments say which entry numbers are in
    it, and the jump between them is where the pause was.
    """
    trace = a_trace_feeding(recorder)
    recorder.start("with-a-gap")
    trace.append("visit", state_name="before")
    recorder.pause()
    trace.append("visit", state_name="during the pause")
    trace.append("visit", state_name="also during the pause")
    recorder.resume()
    trace.append("visit", state_name="after")
    manifest = recorder.stop()

    kept = [entry["state_name"] for entry in recorder.entries_of("with-a-gap")]
    assert kept == ["before", "after"]

    segments = manifest["segments"]
    assert len(segments) == 2, "a pause is two stretches, not one"
    assert segments[0]["to_entry_number"] + 1 != segments[1]["from_entry_number"], (
        "the entry numbers must jump across the pause -- a recording whose numbers "
        "were contiguous would be claiming it saw everything"
    )

    # And the trace, which was never paused, still holds all four.
    assert len(trace.entries_since(0)) == 4


def test_the_entry_numbers_are_the_traces_own(recorder) -> None:
    """So a recording joins back to the trace exactly, rather than approximately."""
    trace = a_trace_feeding(recorder)
    trace.append("visit", state_name="before anybody recorded")
    recorder.start("later")
    trace.append("visit", state_name="first kept")
    recorder.stop()
    assert recorder.entries_of("later")[0]["entry_number"] == 1


def test_clear_keeps_recording_and_stop_keeps_the_file(recorder) -> None:
    """Four verbs, and clear is the one that is not a stop.

    "The last ten minutes were me testing a valve" is a different intention from
    "this recording is finished", and a UI with only stop would make somebody
    delete a file to express it.
    """
    trace = a_trace_feeding(recorder)
    recorder.start("kept")
    trace.append("visit", state_name="the valve test")
    recorder.clear()
    assert recorder.active["state"] == "recording", "clear does not end the recording"
    assert recorder.active["entry_count"] == 0

    trace.append("visit", state_name="the real thing")
    recorder.stop()
    assert [entry["state_name"] for entry in recorder.entries_of("kept")] == ["the real thing"]
    assert [manifest["name"] for manifest in recorder.manifests()] == ["kept"]


def test_a_cleared_recording_says_it_was_cleared(recorder) -> None:
    """The manifest keeps the timestamp, so an empty stretch has an explanation."""
    recorder.start("cleared")
    recorder.clear()
    assert "cleared_host_time" in recorder.manifest_of("cleared")


def test_two_recordings_at_once_are_refused(recorder) -> None:
    recorder.start("first")
    with pytest.raises(RecordingStateRefused) as refusal:
        recorder.start("second")
    assert "first" in str(refusal.value), "the refusal names what is in the way"


def test_recording_over_a_stored_one_is_refused(recorder) -> None:
    """Because the alternative is a morning's recording turning out to be an
    afternoon's, discovered a week later."""
    recorder.start("tuesday")
    recorder.stop()
    with pytest.raises(RecordingNameTaken):
        recorder.start("tuesday")


def test_pausing_nothing_is_refused_with_a_way_out(recorder) -> None:
    with pytest.raises(RecordingStateRefused) as refusal:
        recorder.pause()
    assert "start one" in str(refusal.value).lower()


def test_deleting_the_running_recording_is_refused(recorder) -> None:
    """Deleting the file under a recording would leave the rig writing into
    nothing, which is a hole nobody would see until they read the file back."""
    recorder.start("running")
    with pytest.raises(RecordingStateRefused):
        recorder.delete("running")
    recorder.stop()
    recorder.delete("running")
    assert recorder.manifests() == []


def test_a_name_that_is_not_a_file_name_is_refused(recorder) -> None:
    """A name becomes a path, so this is the traversal check as much as it is a
    tidiness one."""
    for bad in ("../escape", "with/slash", "", " leading"):
        with pytest.raises(BadRecordingName):
            recorder.start(bad)


def test_reading_a_recording_nobody_made(recorder) -> None:
    with pytest.raises(RecordingNotInStore):
        recorder.manifest_of("never-happened")


def test_the_file_is_written_as_it_goes_rather_than_at_the_end(recorder, tmp_path) -> None:
    """Same rule the trace's day file has: a crash must not cost the window that
    mattered, which is everything not yet flushed."""
    trace = a_trace_feeding(recorder)
    recorder.start("crash-proof")
    trace.append("visit", state_name="mid-session")
    written = (tmp_path / "recordings" / "crash-proof.ndjson").read_text()
    assert json.loads(written.splitlines()[-1])["state_name"] == "mid-session"


def test_the_manifest_survives_the_daemon(recorder, tmp_path) -> None:
    """A recording is read back by name after a restart, off disk, with no ring."""
    trace = a_trace_feeding(recorder)
    recorder.start("yesterday")
    trace.append("visit", state_name="a state")
    recorder.stop()

    after_a_restart = EventRecorder(tmp_path / "recordings")
    assert after_a_restart.manifest_of("yesterday")["entry_count"] == 1
    assert after_a_restart.entries_of("yesterday")[0]["state_name"] == "a state"


def test_a_sink_that_throws_cannot_stop_the_trace() -> None:
    """A recording is a convenience; the trace is the thing that must not stop."""
    trace = StateVisitTrace(10)
    trace.add_sink(lambda entry: 1 / 0)
    trace.append("visit", state_name="still recorded")
    assert len(trace.entries_since(0)) == 1
