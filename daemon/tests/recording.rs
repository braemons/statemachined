// SPDX-License-Identifier: AGPL-3.0-or-later
//! Recordings: four verbs, two files, and the gap a pause leaves.
//!
//! Ported from `daemon/tests/unit/test_event_recording.py`, one test for one.

use std::sync::Arc;

use serde_json::{json, Value};
use statemachined::device::state_visit_trace::{fields, StateVisitTrace};
use statemachined::event_recording::{EventRecorder, RecordingProblem};
use tempfile::TempDir;

fn a_recorder() -> (Arc<EventRecorder>, TempDir) {
    let directory = tempfile::tempdir().unwrap();
    let recorder = Arc::new(EventRecorder::new(Some(
        directory.path().join("recordings"),
    )));
    (recorder, directory)
}

fn a_trace_feeding(recorder: &Arc<EventRecorder>) -> StateVisitTrace {
    let trace = StateVisitTrace::new(1000, None);
    let sink = recorder.clone();
    trace.add_sink(Box::new(move |entry| sink.record(entry)));
    trace
}

fn visit(trace: &StateVisitTrace, state_name: &str) {
    trace.append("visit", fields(json!({ "state_name": state_name })));
}

fn state_names(entries: &[Value]) -> Vec<&str> {
    entries
        .iter()
        .map(|entry| entry["state_name"].as_str().unwrap())
        .collect()
}

#[test]
fn nothing_is_captured_until_somebody_starts_one() {
    let (recorder, _directory) = a_recorder();
    let trace = a_trace_feeding(&recorder);
    visit(&trace, "waiting");
    assert!(recorder.active().is_none());
    assert!(recorder.manifests().is_empty());
}

#[test]
fn what_happens_while_recording_is_in_the_file() {
    let (recorder, _directory) = a_recorder();
    let trace = a_trace_feeding(&recorder);
    recorder.start("morning", "", None).unwrap();
    visit(&trace, "waiting");
    visit(&trace, "reward");
    recorder.stop().unwrap();
    let entries = recorder.entries_of("morning", 0, 500).unwrap();
    assert_eq!(state_names(&entries), ["waiting", "reward"]);
    assert_eq!(recorder.manifest_of("morning").unwrap()["entry_count"], 2);
}

#[test]
fn a_pause_leaves_a_gap_that_the_manifest_states() {
    // The trace keeps running through the pause; what must not happen is the
    // recording looking continuous afterwards.
    let (recorder, _directory) = a_recorder();
    let trace = a_trace_feeding(&recorder);
    recorder.start("with-a-gap", "", None).unwrap();
    visit(&trace, "before");
    recorder.pause().unwrap();
    visit(&trace, "during the pause");
    visit(&trace, "also during the pause");
    recorder.resume().unwrap();
    visit(&trace, "after");
    let manifest = recorder.stop().unwrap();

    let kept = recorder.entries_of("with-a-gap", 0, 500).unwrap();
    assert_eq!(state_names(&kept), ["before", "after"]);
    let segments = manifest["segments"].as_array().unwrap();
    assert_eq!(segments.len(), 2, "a pause is two stretches, not one");
    assert_ne!(
        segments[0]["to_entry_number"].as_i64().unwrap() + 1,
        segments[1]["from_entry_number"].as_i64().unwrap(),
        "the entry numbers must jump across the pause"
    );
    assert_eq!(trace.entries_since(0, 1000).len(), 4);
}

#[test]
fn the_entry_numbers_are_the_traces_own() {
    let (recorder, _directory) = a_recorder();
    let trace = a_trace_feeding(&recorder);
    visit(&trace, "before anybody recorded");
    recorder.start("later", "", None).unwrap();
    visit(&trace, "first kept");
    recorder.stop().unwrap();
    assert_eq!(
        recorder.entries_of("later", 0, 500).unwrap()[0]["entry_number"],
        1
    );
}

#[test]
fn clear_keeps_recording_and_stop_keeps_the_file() {
    let (recorder, _directory) = a_recorder();
    let trace = a_trace_feeding(&recorder);
    recorder.start("kept", "", None).unwrap();
    visit(&trace, "the valve test");
    recorder.clear().unwrap();
    let active = recorder.active().unwrap();
    assert_eq!(
        active["state"], "recording",
        "clear does not end the recording"
    );
    assert_eq!(active["entry_count"], 0);
    visit(&trace, "the real thing");
    recorder.stop().unwrap();
    assert_eq!(
        state_names(&recorder.entries_of("kept", 0, 500).unwrap()),
        ["the real thing"]
    );
    let names: Vec<Value> = recorder
        .manifests()
        .iter()
        .map(|m| m["name"].clone())
        .collect();
    assert_eq!(names, [json!("kept")]);
}

#[test]
fn a_cleared_recording_says_it_was_cleared() {
    let (recorder, _directory) = a_recorder();
    recorder.start("cleared", "", None).unwrap();
    recorder.clear().unwrap();
    assert!(recorder
        .manifest_of("cleared")
        .unwrap()
        .contains_key("cleared_host_time"));
}

#[test]
fn two_recordings_at_once_are_refused() {
    let (recorder, _directory) = a_recorder();
    recorder.start("first", "", None).unwrap();
    match recorder.start("second", "", None) {
        Err(RecordingProblem::AlreadyOpen(sentence)) => assert!(sentence.contains("first")),
        other => panic!("{other:?}"),
    }
}

#[test]
fn recording_over_a_stored_one_is_refused() {
    let (recorder, _directory) = a_recorder();
    recorder.start("tuesday", "", None).unwrap();
    recorder.stop().unwrap();
    assert!(matches!(
        recorder.start("tuesday", "", None),
        Err(RecordingProblem::NameTaken(_))
    ));
}

#[test]
fn pausing_nothing_is_refused_with_a_way_out() {
    let (recorder, _directory) = a_recorder();
    match recorder.pause() {
        Err(RecordingProblem::StateRefused(sentence)) => {
            assert!(sentence.to_lowercase().contains("start one"))
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn deleting_the_running_recording_is_refused() {
    let (recorder, _directory) = a_recorder();
    recorder.start("running", "", None).unwrap();
    assert!(matches!(
        recorder.delete("running"),
        Err(RecordingProblem::InProgress(_))
    ));
    recorder.stop().unwrap();
    recorder.delete("running").unwrap();
    assert!(recorder.manifests().is_empty());
}

#[test]
fn a_name_that_is_not_a_file_name_is_refused() {
    let (recorder, _directory) = a_recorder();
    for bad in ["../escape", "with/slash", "", " leading", "trailing\n"] {
        assert!(
            matches!(
                recorder.start(bad, "", None),
                Err(RecordingProblem::BadName(_))
            ),
            "{bad:?} was taken"
        );
    }
}

#[test]
fn reading_a_recording_nobody_made() {
    let (recorder, _directory) = a_recorder();
    assert!(matches!(
        recorder.manifest_of("never-happened"),
        Err(RecordingProblem::NotInStore(_))
    ));
}

#[test]
fn the_file_is_written_as_it_goes_rather_than_at_the_end() {
    let (recorder, directory) = a_recorder();
    let trace = a_trace_feeding(&recorder);
    recorder.start("crash-proof", "", None).unwrap();
    visit(&trace, "mid-session");
    let written =
        std::fs::read_to_string(directory.path().join("recordings/crash-proof.ndjson")).unwrap();
    let last: Value = serde_json::from_str(written.lines().last().unwrap()).unwrap();
    assert_eq!(last["state_name"], "mid-session");
}

#[test]
fn the_manifest_survives_the_daemon() {
    let (recorder, directory) = a_recorder();
    let trace = a_trace_feeding(&recorder);
    recorder.start("yesterday", "", None).unwrap();
    visit(&trace, "a state");
    recorder.stop().unwrap();
    let after_a_restart = EventRecorder::new(Some(directory.path().join("recordings")));
    assert_eq!(
        after_a_restart.manifest_of("yesterday").unwrap()["entry_count"],
        1
    );
    assert_eq!(
        after_a_restart.entries_of("yesterday", 0, 500).unwrap()[0]["state_name"],
        "a state"
    );
}

#[test]
fn the_manifest_is_written_as_python_writes_it() {
    // Two-space indent, `": "`, keys in the order Python's dict has them.
    let (recorder, directory) = a_recorder();
    recorder.start("shape", "Fütterung", None).unwrap();
    let text =
        std::fs::read_to_string(directory.path().join("recordings/shape.recording.json")).unwrap();
    assert!(
        text.starts_with("{\n  \"name\": \"shape\",\n  \"description\": \"F\\u00fctterung\",\n")
    );
    assert!(
        text.ends_with("  \"entry_count\": 0,\n  \"kind_counts\": {}\n}\n"),
        "{text}"
    );
}
