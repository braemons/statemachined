// SPDX-License-Identifier: AGPL-3.0-or-later
//! The ring, the cursor, and the one boundary a caller has to be told about.
//!
//! Ported from `daemon/tests/unit/test_state_visit_trace.py`, one test for one.

use serde_json::{json, Value};
use statemachined::device::state_visit_trace::{fields, StateVisitTrace, KIND_STATE_VISIT};

fn trial(trial_id: i64) -> serde_json::Map<String, Value> {
    fields(json!({ "trial_id": trial_id }))
}

#[test]
fn entries_are_numbered_by_the_daemon_not_by_the_device() {
    // The device's `seq` restarts at zero every trial, so it cannot address a
    // position in a log that spans a session.
    let trace = StateVisitTrace::new(10, None);
    let first = trace.append(
        KIND_STATE_VISIT,
        fields(json!({"trial_id": 1, "device_sequence_number": 0})),
    );
    let second = trace.append(
        KIND_STATE_VISIT,
        fields(json!({"trial_id": 2, "device_sequence_number": 0})),
    );
    assert_eq!(first["entry_number"], 0);
    assert_eq!(second["entry_number"], 1);
    assert_eq!(second["device_sequence_number"], 0);
}

#[test]
fn a_cursor_returns_what_came_after_it() {
    let trace = StateVisitTrace::new(10, None);
    for index in 0..5 {
        trace.append(KIND_STATE_VISIT, trial(index));
    }
    let after: Vec<Value> = trace
        .entries_since(3, 1000)
        .iter()
        .map(|e| e["trial_id"].clone())
        .collect();
    assert_eq!(after, [json!(3), json!(4)]);
}

#[test]
fn the_ring_drops_the_oldest_and_says_which() {
    let trace = StateVisitTrace::new(3, None);
    for index in 0..5 {
        trace.append(KIND_STATE_VISIT, trial(index));
    }
    assert_eq!(trace.len(), 3);
    assert_eq!(trace.oldest_entry_number_still_held(), 2);
    assert_eq!(trace.newest_entry_number(), 4);
}

#[test]
fn a_cursor_that_has_fallen_out_of_the_ring_is_detectable() {
    let trace = StateVisitTrace::new(3, None);
    for index in 0..5 {
        trace.append(KIND_STATE_VISIT, trial(index));
    }
    assert!(trace.has_fallen_out_of_the_ring(0));
    assert!(!trace.has_fallen_out_of_the_ring(2));
}

#[test]
fn an_empty_trace_has_lost_nothing_and_holds_from_zero() {
    let trace = StateVisitTrace::new(3, None);
    assert!(!trace.has_fallen_out_of_the_ring(0));
    assert_eq!(trace.oldest_entry_number_still_held(), 0);
    assert_eq!(trace.newest_entry_number(), -1);
}

#[test]
fn one_trial_can_be_asked_for_on_its_own() {
    let trace = StateVisitTrace::new(10, None);
    for trial_id in [1, 2, 1] {
        trace.append(KIND_STATE_VISIT, trial(trial_id));
    }
    assert_eq!(trace.entries_for_trial(1).len(), 2);
}

#[test]
fn every_entry_is_written_to_the_file_as_it_arrives() {
    // On arrival and not on eviction: a crash otherwise loses exactly the
    // window that mattered most.
    let directory = tempfile::tempdir().unwrap();
    let trace = StateVisitTrace::new(2, Some(directory.path().to_path_buf()));
    for index in 0..5 {
        trace.append(KIND_STATE_VISIT, trial(index));
    }
    let written: Vec<_> = std::fs::read_dir(directory.path())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| {
            path.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("trace-")
        })
        .collect();
    assert_eq!(written.len(), 1);
    let lines: Vec<Value> = std::fs::read_to_string(&written[0])
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    let trial_ids: Vec<i64> = lines
        .iter()
        .map(|line| line["trial_id"].as_i64().unwrap())
        .collect();
    assert_eq!(trial_ids, [0, 1, 2, 3, 4]);
}

#[test]
fn a_line_is_written_the_way_python_writes_it() {
    // Same keys in the same order, no spaces, and non-ASCII escaped — so a
    // day's file does not change shape because the daemon writing it did.
    let directory = tempfile::tempdir().unwrap();
    let trace = StateVisitTrace::new(2, Some(directory.path().to_path_buf()));
    trace.append(
        "session_opened",
        fields(json!({"graph_names": ["Fütterung"], "set_version": 3})),
    );
    let file = std::fs::read_dir(directory.path())
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let line = std::fs::read_to_string(file).unwrap();
    assert!(line.starts_with(r#"{"entry_number":0,"kind":"session_opened","recorded_host_time":""#));
    let expected_end = concat!(
        "Z\",\"graph_names\":[\"F",
        "\\",
        "u00fctterung\"],\"set_version\":3}\n"
    );
    assert!(line.ends_with(expected_end), "{line}");
}

#[test]
fn a_disk_that_cannot_be_written_does_not_stop_a_session() {
    let directory = tempfile::tempdir().unwrap();
    let in_the_way = directory.path().join("file-in-the-way");
    std::fs::write(&in_the_way, "not a directory").unwrap();
    let trace = StateVisitTrace::new(10, Some(in_the_way.join("trace")));
    trace.append(KIND_STATE_VISIT, trial(1));
    assert_eq!(trace.len(), 1);
}

#[test]
fn every_entry_carries_the_host_time_it_was_recorded_at() {
    let trace = StateVisitTrace::new(10, None);
    let entry = trace.append(KIND_STATE_VISIT, trial(1));
    let time = entry["recorded_host_time"].as_str().unwrap();
    assert!(time.ends_with('Z') && time.contains('T'));
    assert_eq!(time.len(), "2023-11-14T22:13:20.000000Z".len());
}

#[test]
fn a_sink_sees_every_entry_in_order_and_one_that_fails_stops_nothing() {
    use std::sync::{Arc, Mutex};
    let trace = StateVisitTrace::new(2, None);
    let seen = Arc::new(Mutex::new(Vec::new()));
    let into = seen.clone();
    trace.add_sink(Box::new(|_| panic!("a sink that cannot write")));
    trace.add_sink(Box::new(move |entry| {
        into.lock().unwrap().push(entry["entry_number"].clone())
    }));
    for index in 0..4 {
        trace.append(KIND_STATE_VISIT, trial(index));
    }
    assert_eq!(
        *seen.lock().unwrap(),
        [json!(0), json!(1), json!(2), json!(3)]
    );
}
