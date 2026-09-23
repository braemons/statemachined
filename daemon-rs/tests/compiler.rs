// SPDX-License-Identifier: AGPL-3.0-or-later
//! The graph-set compiler, against what Python uploaded.
//!
//! **The upload is where a wrong index stops being visible.** A mask one bit
//! out, a target one state out, a distribution at pool entry 2 instead of 3:
//! each is a valid message, uploads cleanly, and runs a different experiment.
//! The device validates shape, not intent. So `tools/graph_set_cases.py` records
//! every message Python would send, and this checks that Rust sends the same
//! ones, in the same order, with the same fields — and builds the same tables a
//! result is read back through.
//!
//! Refusals are compared by their sentence as well as by whether. Unlike a
//! document's parse error, these are written by hand on both sides, and the
//! sentence is the contract: it is what names the state to fix.

use indexmap::IndexMap;
use serde::Deserialize;
use serde_json::{Map as JsonMap, Value as Json};
use statemachined::graph_set_compiler::{
    compile_graph_set_for_device, CompileError, CompiledGraphSet, DeviceCapabilities,
};
use statemachined::model::graph_definition::GraphDefinition;
use statemachined::model::line_map::LineMap;

#[derive(Debug, Deserialize)]
struct Case {
    why: String,
    graphs: Vec<GraphDefinition>,
    line_map: LineMap,
    hello_ack: Json,
    set_version: i64,
    answer: Answer,
}

#[derive(Debug, Deserialize)]
struct Answer {
    compiled: Option<Compiled>,
    refused: Option<String>,
}

#[derive(Debug, Deserialize, PartialEq)]
struct Compiled {
    set_version: i64,
    upload_messages: Vec<Message>,
    graphs_by_slot: Vec<Graph>,
    pool_usage: IndexMap<String, i64>,
    pool_capacity: IndexMap<String, i64>,
}

#[derive(Debug, Deserialize, PartialEq)]
struct Message {
    msg_type: String,
    fields: JsonMap<String, Json>,
}

#[derive(Debug, Deserialize, PartialEq)]
struct Graph {
    name: String,
    slot: usize,
    state_names_by_index: Vec<String>,
    transition_target_names_by_state_index: Vec<Vec<String>>,
    distribution_pool_index_by_name: IndexMap<String, usize>,
}

impl From<&CompiledGraphSet> for Compiled {
    fn from(compiled: &CompiledGraphSet) -> Self {
        Compiled {
            set_version: compiled.set_version,
            upload_messages: compiled
                .upload_messages
                .iter()
                .map(|message| Message {
                    msg_type: message.msg_type.as_str().to_string(),
                    fields: message.fields.clone(),
                })
                .collect(),
            graphs_by_slot: compiled
                .graphs_by_slot
                .iter()
                .map(|graph| Graph {
                    name: graph.name.clone(),
                    slot: graph.slot,
                    state_names_by_index: graph.state_names_by_index.clone(),
                    transition_target_names_by_state_index: graph
                        .transition_target_names_by_state_index
                        .clone(),
                    distribution_pool_index_by_name: graph.distribution_pool_index_by_name.clone(),
                })
                .collect(),
            pool_usage: compiled.pool_usage.clone(),
            pool_capacity: compiled.pool_capacity.clone(),
        }
    }
}

fn cases() -> Vec<Case> {
    serde_json::from_str(include_str!("graph_set_cases.json"))
        .expect("the cases tools/graph_set_cases.py writes")
}

fn compile(case: &Case) -> Result<CompiledGraphSet, CompileError> {
    compile_graph_set_for_device(
        &case.graphs,
        &case.line_map,
        &DeviceCapabilities::from_hello_ack(&case.hello_ack),
        case.set_version,
    )
}

#[test]
fn the_cases_cover_both_answers_and_every_upload_message() {
    let cases = cases();
    assert!(cases.len() > 30, "only {} cases", cases.len());
    assert!(cases.iter().any(|case| case.answer.refused.is_some()));
    let kinds: std::collections::BTreeSet<&str> = cases
        .iter()
        .filter_map(|case| case.answer.compiled.as_ref())
        .flat_map(|compiled| compiled.upload_messages.iter())
        .map(|message| message.msg_type.as_str())
        .collect();
    for kind in [
        "set_begin",
        "graph_dist",
        "graph_begin",
        "graph_timer",
        "graph_state",
        "graph_transition",
        "graph_action",
        "graph_end",
        "set_end",
    ] {
        assert!(kinds.contains(kind), "no case uploads a {kind}");
    }
}

#[test]
fn every_case_compiles_or_refuses_the_way_python_did() {
    let mut disagreed: Vec<String> = Vec::new();
    for case in cases() {
        let ours = compile(&case);
        match (&case.answer.compiled, &case.answer.refused, &ours) {
            (Some(theirs), None, Ok(ours)) => {
                let ours = Compiled::from(ours);
                if ours.upload_messages.len() != theirs.upload_messages.len() {
                    disagreed.push(format!(
                        "{}: {} messages, Python sent {}",
                        case.why,
                        ours.upload_messages.len(),
                        theirs.upload_messages.len()
                    ));
                    continue;
                }
                // The first message that differs, not the whole upload: one
                // wrong index shifts nothing else, and is easier read alone.
                if let Some((position, (mine, python))) = ours
                    .upload_messages
                    .iter()
                    .zip(&theirs.upload_messages)
                    .enumerate()
                    .find(|(_, (mine, python))| mine != python)
                {
                    disagreed.push(format!(
                        "{}: message {position}\n    rust:   {mine:?}\n    python: {python:?}",
                        case.why
                    ));
                    continue;
                }
                if &ours != theirs {
                    disagreed.push(format!(
                        "{}: the same upload, different tables\n    rust:   {:?} {:?} {:?}\n    python: {:?} {:?} {:?}",
                        case.why,
                        ours.graphs_by_slot,
                        ours.pool_usage,
                        ours.pool_capacity,
                        theirs.graphs_by_slot,
                        theirs.pool_usage,
                        theirs.pool_capacity,
                    ));
                }
            }
            (None, Some(theirs), Err(CompileError::Set(ours))) => {
                if ours != theirs {
                    disagreed.push(format!(
                        "{}: refused in other words\n    rust:   {ours}\n    python: {theirs}",
                        case.why
                    ));
                }
            }
            (_, _, ours) => disagreed.push(format!(
                "{}: python {}, rust {:?}",
                case.why,
                case.answer
                    .refused
                    .as_deref()
                    .map(|sentence| format!("refused ({sentence})"))
                    .unwrap_or_else(|| "compiled".into()),
                ours.as_ref().map(|_| "compiled")
            )),
        }
    }
    assert!(
        disagreed.is_empty(),
        "{} of the cases disagree:\n  {}",
        disagreed.len(),
        disagreed.join("\n  ")
    );
}

#[test]
fn a_graph_the_set_does_not_hold_is_not_a_set_that_does_not_fit() {
    // Its own variant, because it is a different thing to fix: the set compiled
    // perfectly, and the fix is to commit one that contains the graph.
    let case = cases()
        .into_iter()
        .find(|case| case.why.starts_with("all three shipped graphs"))
        .expect("the three-graph case");
    let compiled = compile(&case).expect("it compiles");
    assert_eq!(compiled.slot_for_graph_name("state-walk"), Ok(1));
    match compiled.slot_for_graph_name("nobody") {
        Err(CompileError::NotInSet(sentence)) => {
            assert!(sentence.contains("go-nogo, state-walk, two-alternative-forced-choice"))
        }
        other => panic!("expected NotInSet, got {other:?}"),
    }
}

#[test]
fn capabilities_are_read_from_where_the_greeting_puts_them() {
    // The pools are under `caps`; the line counts are beside it. A greeting
    // naming `max_states` at the top level is not declaring it.
    let greeting = serde_json::json!({
        "caps": {"max_states": 12, "max_graphs": 3, "first_timer_line": 16},
        "max_transitions": 99,
        "n_input_lines": 8,
        "n_output_lines": 4,
    });
    let capabilities = DeviceCapabilities::from_hello_ack(&greeting);
    assert_eq!(capabilities.max_states, 12);
    assert_eq!(capabilities.max_graphs, 3);
    assert_eq!(capabilities.first_timer_line, 16);
    assert_eq!(capabilities.max_transitions, 0);
    assert_eq!(capabilities.input_line_count, 8);
    assert_eq!(capabilities.output_line_count, 4);
}
