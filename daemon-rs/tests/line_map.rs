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
//! treatment the documents got: `line_map_cases.json` and `wiring_cases.json`
//! hold what Python said, recorded before that daemon was retired, and this checks that Rust says it too.

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
        .expect("the cases recorded from the Python daemon")
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

// -- the masks -----------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct WiringCase {
    why: String,
    pin_map: DevicePinMap,
    line_map: LineMap,
    answer: WiringAnswer,
}

#[derive(Debug, Deserialize)]
struct WiringAnswer {
    wiring: WiringFields,
}

/// The shape Python's `wiring_message_fields` returns.
#[derive(Debug, Deserialize, PartialEq, Eq)]
struct WiringFields {
    invert: u32,
    enable: u32,
    safe: u32,
    debounce_ms: Vec<i64>,
}

fn wiring_cases() -> Vec<WiringCase> {
    serde_json::from_str(include_str!("wiring_cases.json"))
        .expect("the cases recorded from the Python daemon")
}

#[test]
fn every_wiring_body_is_the_one_python_would_push() {
    // **A mask is where an off-by-one stops being visible.** `invert`, `enable`
    // and `safe` are bit positions over line numbers; a map resolved one line
    // out produces a mask that is a *valid* mask, pushes without complaint, and
    // conditions the wrong pin. Nothing after this point can tell.
    let mut disagreed: Vec<String> = Vec::new();
    for case in wiring_cases() {
        let resolved = case
            .line_map
            .resolved_against(&case.pin_map)
            .unwrap_or_else(|problem| panic!("{}: {problem}", case.why));
        let ours = resolved
            .wiring_message_fields()
            .unwrap_or_else(|problem| panic!("{}: {problem}", case.why));
        let mine = WiringFields {
            invert: ours.invert,
            enable: ours.enable,
            safe: ours.safe,
            debounce_ms: ours.debounce_ms.clone(),
        };
        if mine != case.answer.wiring {
            disagreed.push(format!(
                "{}: {mine:?} against {:?}",
                case.why, case.answer.wiring
            ));
        }
    }
    assert!(disagreed.is_empty(), "{}", disagreed.join("\n  "));
}

#[test]
fn an_unresolved_line_is_refused_rather_than_masked_at_zero() {
    // A line configured by pin alone has no index until the board has been
    // asked. Everything below this deals in masks, so "not yet resolved" has to
    // stop being representable here -- treating it as line 0 would put a
    // lever's conditioning on whatever line 0 happens to be.
    let line_map: LineMap = serde_json::from_value(serde_json::json!({
        "input_lines": [{"name": "lever", "pin_label": "A0"}]
    }))
    .expect("a line map");
    let problem = line_map
        .wiring_message_fields()
        .expect_err("an unresolved line has no mask");
    assert!(problem.to_string().contains("lever"), "{problem}");
    assert!(problem.to_string().contains("resolved"), "{problem}");
}

#[test]
fn a_pin_prints_as_something_a_person_can_hold_against_the_wires() {
    use statemachined::device::board_pin_labels::{high_lines, pin_label, word_bits};
    use statemachined::device::device_pin_map::Direction;

    assert_eq!(pin_label("uno_r4_minima", Direction::In, 0), "0 (D2)");
    assert_eq!(pin_label("uno_r4_minima", Direction::Out, 3), "3 (A0)");
    // A board this table does not know prints bare numbers rather than a
    // plausible lie about somebody else's pinout.
    assert_eq!(pin_label("some_other_mcu", Direction::In, 0), "0");
    assert_eq!(pin_label("uno_r4_minima", Direction::In, 99), "99");

    // Line 0 on the left, which is the opposite of a binary literal and the
    // same as the hardware table. Anybody comparing this against a breadboard
    // is counting from line 0.
    assert_eq!(word_bits(0b0000_0101, 8), "1.1.....");
    assert_eq!(high_lines("uno_r4_minima", Direction::In, 0b0101, 8), "0 (D2), 2 (D4)");
    assert_eq!(high_lines("uno_r4_minima", Direction::In, 0, 8), "none");
}
