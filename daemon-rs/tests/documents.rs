// SPDX-License-Identifier: AGPL-3.0-or-later
//! Both implementations, over the same documents.
//!
//! **The documents are user data** (`RUST_REWRITE_PLAN.md` §4.2). A graph file
//! somebody wrote last year lives in `/var/lib/statemachined/graphs/` and must
//! parse identically under the Python daemon and this one. So must a file that
//! is *refused*: refusals are half the contract, and the half that is easy to
//! forget — a port that accepted everything would pass a round-trip test and
//! lose every rule in `graph_definition.py`.
//!
//! `tools/document_corpus.py` writes `document_corpus.json`: the real graphs in
//! `graphs/`, plus one systematic mutation per refusal rule, each with what the
//! Python implementation said about it. This checks that the Rust
//! implementation says the same.
//!
//! **What is compared, and what is not.** That each document is accepted or
//! refused, and — for an accepted one — that both parsed it to the same
//! structure. Not the wording of a refusal: serde's parse errors are not
//! pydantic's, and holding two libraries to one sentence would be a test about
//! string formatting. What matters is that the same documents get through.

use std::collections::BTreeMap;

use serde::Deserialize;
use statemachined::model::graph_definition::GraphDefinition;
use statemachined::model::state_machine_config::StateMachineConfig;

#[derive(Debug, Deserialize)]
struct Case {
    name: String,
    document: serde_json::Value,
    accepted: bool,
    #[serde(default)]
    refusal: Option<String>,
    #[serde(default)]
    parsed: Option<serde_json::Value>,
}

fn graphs() -> Vec<Case> {
    serde_json::from_str(include_str!("graph_corpus.json"))
        .expect("the corpus tools/document_corpus.py writes")
}

fn configs() -> Vec<Case> {
    serde_json::from_str(include_str!("config_corpus.json"))
        .expect("the corpus tools/document_corpus.py writes")
}

/// Both corpora, each with the parser that owns it.
///
/// A closure rather than an enum: what varies between the two is one function
/// call, and the rest of every test below is identical.
#[allow(clippy::type_complexity)]
fn both() -> Vec<(&'static str, Vec<Case>, Box<dyn Fn(&str) -> Result<serde_json::Value, String>>)> {
    vec![
        (
            "graph",
            graphs(),
            Box::new(|text: &str| {
                GraphDefinition::from_json(text)
                    .map_err(|problem| problem.to_string())
                    .and_then(|graph| {
                        serde_json::to_value(&graph).map_err(|problem| problem.to_string())
                    })
            }),
        ),
        (
            "config",
            configs(),
            Box::new(|text: &str| {
                StateMachineConfig::from_json(text)
                    .map_err(|problem| problem.to_string())
                    .and_then(|config| {
                        serde_json::to_value(&config).map_err(|problem| problem.to_string())
                    })
            }),
        ),
    ]
}

#[test]
fn the_corpora_are_not_empty_and_cover_both_answers() {
    // A guard on the guard: a corpus that stopped being generated, or that
    // only held documents of one kind, would make every test below vacuous.
    for (kind, cases, _) in both() {
        assert!(cases.len() > 20, "only {} {kind} documents", cases.len());
        assert!(cases.iter().any(|case| case.accepted), "no {kind} is accepted");
        assert!(cases.iter().any(|case| !case.accepted), "no {kind} is refused");
    }
}

#[test]
fn every_document_gets_the_same_answer_from_both() {
    let mut disagreed: Vec<String> = Vec::new();
    for (kind, cases, parse) in both() {
        for case in cases {
            let text = serde_json::to_string(&case.document).expect("it came from json");
            match (case.accepted, parse(&text)) {
                (true, Err(problem)) => disagreed.push(format!(
                    "{kind} {}: python accepted it, rust refused it -- {problem}",
                    case.name
                )),
                (false, Ok(_)) => disagreed.push(format!(
                    "{kind} {}: python refused it ({}), rust accepted it",
                    case.name,
                    case.refusal.as_deref().unwrap_or("?").lines().next().unwrap_or("")
                )),
                _ => {}
            }
        }
    }
    assert!(
        disagreed.is_empty(),
        "{} documents disagreed:\n  {}",
        disagreed.len(),
        disagreed.join("\n  ")
    );
}

#[test]
fn an_accepted_document_parses_to_the_same_thing() {
    // Agreeing to accept a file is not enough: two parsers can both say yes and
    // disagree about what a field meant. This compares the round trip.
    let mut wrong: Vec<String> = Vec::new();
    for (kind, cases, parse) in both() {
        for case in cases {
            let (Some(expected), true) = (&case.parsed, case.accepted) else {
                continue;
            };
            let text = serde_json::to_string(&case.document).expect("it came from json");
            let mine = parse(&text).expect("the previous test says it is accepted");
            if let Some(difference) = first_difference(expected, &mine, "") {
                wrong.push(format!("{kind} {}: {difference}", case.name));
            }
        }
    }
    assert!(wrong.is_empty(), "{}", wrong.join("\n  "));
}

/// Where two documents first differ, as a path and the two values.
///
/// A whole-value assertion on a graph prints two screens of JSON and leaves
/// somebody to find the one field, which is the difference between a test that
/// reports a bug and one that reports that there is a bug.
fn first_difference(
    expected: &serde_json::Value,
    actual: &serde_json::Value,
    path: &str,
) -> Option<String> {
    use serde_json::Value;
    match (expected, actual) {
        (Value::Object(left), Value::Object(right)) => {
            let left: BTreeMap<_, _> = left.iter().collect();
            let right: BTreeMap<_, _> = right.iter().collect();
            for (key, value) in &left {
                match right.get(key) {
                    None => return Some(format!("{path}/{key} is missing")),
                    Some(other) => {
                        if let Some(found) =
                            first_difference(value, other, &format!("{path}/{key}"))
                        {
                            return Some(found);
                        }
                    }
                }
            }
            for key in right.keys() {
                if !left.contains_key(*key) {
                    return Some(format!("{path}/{key} is unexpected"));
                }
            }
            None
        }
        (Value::Array(left), Value::Array(right)) => {
            if left.len() != right.len() {
                return Some(format!(
                    "{path} has {} entries, expected {}",
                    right.len(),
                    left.len()
                ));
            }
            left.iter()
                .zip(right)
                .enumerate()
                .find_map(|(at, (l, r))| first_difference(l, r, &format!("{path}[{at}]")))
        }
        _ if expected == actual => None,
        _ => Some(format!("{path}: expected {expected}, got {actual}")),
    }
}
