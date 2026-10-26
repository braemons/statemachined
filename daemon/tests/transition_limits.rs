// SPDX-License-Identifier: AGPL-3.0-or-later
//! A transition's `limit`: "take `goto` this many times in a trial, then go to
//! `then`".
//!
//! Not in the recorded corpora, because the Python daemon they were recorded
//! from never had limits. So this checks the three places a limit has to
//! survive on its own: the document's rules, the upload message, and the
//! table a visit is read back through.

use serde_json::{json, Value as Json};
use statemachined::graph_set_compiler::{compile_graph_set_for_device, DeviceCapabilities};
use statemachined::model::graph_definition::GraphDefinition;
use statemachined::model::line_map::LineMap;

/// Three early licks restart the wait; the fourth ends the trial.
fn early_lick_graph(limit: Json) -> Json {
    json!({
        "name": "early-lick-retries",
        "entry": "WaitForSpeed",
        "distributions": { "wait": { "kind": "fixed", "duration_ms": 1000 } },
        "states": [
            {
                "name": "WaitForSpeed",
                "timeout": { "after": "wait", "goto": "Go" },
                "transitions": [
                    { "when": { "any": ["lick"] }, "goto": "WaitForSpeed", "limit": limit }
                ]
            },
            { "name": "Go", "outcome": "HIT" },
            { "name": "Early", "outcome": "EARLY" }
        ]
    })
}

fn parse(document: &Json) -> Result<GraphDefinition, String> {
    GraphDefinition::from_json(&document.to_string()).map_err(|problem| problem.to_string())
}

#[test]
fn a_state_reached_only_past_a_limit_is_reachable() {
    parse(&early_lick_graph(json!({ "times": 3, "then": "Early" })))
        .expect("Early is reached past the limit, and only from there");
}

#[test]
fn a_limit_to_a_state_that_does_not_exist_is_refused_by_name() {
    let refused = parse(&early_lick_graph(json!({ "times": 3, "then": "Erly" }))).unwrap_err();
    assert!(refused.contains("'Erly'"), "{refused}");
}

#[test]
fn a_limit_of_zero_or_past_a_byte_is_refused() {
    for times in [0, 256] {
        let refused = parse(&early_lick_graph(
            json!({ "times": times, "then": "Early" }),
        ))
        .unwrap_err();
        assert!(refused.contains("between 1 and 255"), "{times}: {refused}");
    }
}

#[test]
fn a_limit_reaches_the_wire_and_the_table_a_visit_is_read_through() {
    let graph = parse(&early_lick_graph(json!({ "times": 3, "then": "Early" }))).unwrap();
    let line_map: LineMap = serde_json::from_value(json!({
        "input_lines": [{ "name": "lick", "line_index": 0 }],
        "output_lines": []
    }))
    .unwrap();
    let hello_ack = json!({
        "caps": { "max_line": 512, "max_states": 32, "max_transitions": 64,
                  "max_output_actions": 64, "max_distributions": 32,
                  "max_choice_options": 32, "max_path": 255, "max_graphs": 20,
                  "max_timers": 8, "first_timer_line": 24 },
        "n_input_lines": 8, "n_output_lines": 8
    });
    let compiled = compile_graph_set_for_device(
        &[graph],
        &line_map,
        &DeviceCapabilities::from_hello_ack(&hello_ack),
        1,
    )
    .unwrap();

    let transition = compiled
        .upload_messages
        .iter()
        .find(|message| message.msg_type.as_str() == "graph_transition")
        .expect("one transition");
    assert_eq!(transition.fields["target"], json!(0));
    assert_eq!(transition.fields["limit"], json!(3));
    assert_eq!(transition.fields["limit_target"], json!(2));

    let tables = &compiled.graphs_by_slot[0];
    assert_eq!(
        tables.limit_target_names_by_state_index,
        vec![vec![Some("Early".to_string())], vec![], vec![]]
    );
}
