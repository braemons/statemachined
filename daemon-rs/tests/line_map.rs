// SPDX-License-Identifier: AGPL-3.0-or-later
//! Line-map resolution, against what Python answered.
//!
//! **The silent wrong-valve bug lives here.** A line saying `pin_label = "A0"`
//! and `line_index = 4` when the board's A0 is line 5 looks right in every
//! listing, pushes cleanly, uploads cleanly, and drives the wrong pin. Nothing
//! downstream notices, and the first sign is an animal being rewarded by a lamp.
//!
//! So this is the one place in the device layer where a disagreement between the
//! two daemons would stay invisible until it mattered, and it gets the same
//! treatment the documents got: `tools/line_map_cases.py` records what Python
//! says, and this checks that Rust says it too.

use serde::Deserialize;
use statemachined::device::device_pin_map::DevicePinMap;
use statemachined::model::line_map::LineMap;

#[derive(Debug, Deserialize)]
struct Case {
    why: String,
    pin_map: DevicePinMap,
    line_map: LineMap,
    answer: Answer,
}

#[derive(Debug, Deserialize)]
struct Answer {
    resolved: Option<Resolved>,
    /// Python's sentence, when it refused. Compared for *whether*, not for
    /// wording — two languages phrasing one refusal identically is a test
    /// about string formatting.
    refused: Option<String>,
}

#[derive(Debug, Deserialize)]
struct Resolved {
    input_lines: Vec<ResolvedLine>,
    output_lines: Vec<ResolvedLine>,
}

#[derive(Debug, Deserialize, PartialEq)]
struct ResolvedLine {
    name: String,
    line_index: Option<i64>,
}

fn cases() -> Vec<Case> {
    serde_json::from_str(include_str!("line_map_cases.json"))
        .expect("the cases tools/line_map_cases.py writes")
}

#[test]
fn the_cases_cover_both_answers_and_both_kinds_of_map() {
    let cases = cases();
    assert!(cases.len() > 12, "only {} cases", cases.len());
    assert!(cases.iter().any(|case| case.answer.refused.is_some()));
    assert!(cases.iter().any(|case| case.answer.resolved.is_some()));
    assert!(
        cases.iter().any(|case| case.pin_map.came_from_the_device()),
        "no case where the board answered"
    );
    assert!(
        cases.iter().any(|case| !case.pin_map.came_from_the_device()),
        "no case where the map was assumed"
    );
}

#[test]
fn every_case_resolves_or_refuses_the_way_python_did() {
    let mut disagreed: Vec<String> = Vec::new();
    for case in cases() {
        let ours = case.line_map.resolved_against(&case.pin_map);
        match (&case.answer.resolved, &case.answer.refused, ours) {
            (Some(theirs), None, Ok(ours)) => {
                let mine: Vec<ResolvedLine> = ours
                    .input_lines
                    .iter()
                    .map(|line| ResolvedLine {
                        name: line.name.clone(),
                        line_index: line.line_index,
                    })
                    .collect();
                if mine != theirs.input_lines {
                    disagreed.push(format!(
                        "{}: inputs {mine:?} against {:?}",
                        case.why, theirs.input_lines
                    ));
                    continue;
                }
                let mine: Vec<ResolvedLine> = ours
                    .output_lines
                    .iter()
                    .map(|line| ResolvedLine {
                        name: line.name.clone(),
                        line_index: line.line_index,
                    })
                    .collect();
                if mine != theirs.output_lines {
                    disagreed.push(format!(
                        "{}: outputs {mine:?} against {:?}",
                        case.why, theirs.output_lines
                    ));
                }
            }
            (None, Some(_), Err(_)) => {}
            (Some(_), None, Err(problem)) => {
                disagreed.push(format!("{}: python resolved it, rust refused -- {problem}", case.why))
            }
            (None, Some(theirs), Ok(_)) => disagreed.push(format!(
                "{}: python refused it ({}), rust resolved it",
                case.why,
                theirs.lines().next().unwrap_or("")
            )),
            _ => disagreed.push(format!("{}: the case itself is malformed", case.why)),
        }
    }
    assert!(
        disagreed.is_empty(),
        "{} of {} cases disagreed:\n  {}",
        disagreed.len(),
        cases().len(),
        disagreed.join("\n  ")
    );
}

#[test]
fn a_disagreement_between_pin_and_index_names_both_and_says_why() {
    // The refusal's *content* matters here in a way it does not elsewhere: the
    // person reading it has a board in front of them and has to work out which
    // of the two they wrote down wrong. A refusal saying only "bad line map"
    // would leave them guessing.
    let pin_map = DevicePinMap {
        input_pin_labels: vec!["D2".into(), "D3".into(), "A0".into(), "A1".into()],
        output_pin_labels: vec![],
        source: statemachined::device::device_pin_map::PinLabelSource::Device,
    };
    let line_map: LineMap = serde_json::from_value(serde_json::json!({
        "input_lines": [{"name": "lever", "line_index": 3, "pin_label": "A0"}]
    }))
    .expect("a line map");

    let refusal = line_map
        .resolved_against(&pin_map)
        .expect_err("a disagreement is refused")
        .to_string();
    assert!(refusal.contains("lever"), "{refusal}");
    assert!(refusal.contains("line 3"), "{refusal}");
    assert!(refusal.contains("'A0'"), "{refusal}");
    // The board's own answer for both, so the person can see which is which.
    assert!(refusal.contains("'A1'"), "{refusal}");
    assert!(refusal.contains("is line 2"), "{refusal}");
    assert!(
        refusal.contains("nothing downstream would notice"),
        "the refusal should say why it is worth stopping for: {refusal}"
    );
}
