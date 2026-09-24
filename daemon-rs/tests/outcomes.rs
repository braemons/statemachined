// SPDX-License-Identifier: AGPL-3.0-or-later
//! Every copy of the `.tdr` taxonomy in this repository, against the enum.
//!
//! `proto/braemons/v1/trial_outcome.proto` **is** the taxonomy, vendored
//! byte-identically from `contracts/vendored/proto/`: this device reports an
//! outcome and triald records one, and neither owns the list. Three places here
//! restate it, and each is held to it:
//!
//! * `firmware/core/trial/trial.h` — the C++ enum the board stores and reports.
//!   The *number* is the wire contract; `Hit` for `HIT` is local style.
//! * `model::trial_outcome::declarable` — the outcomes a graph may name, which
//!   is the taxonomy less `NOT_DECLARABLE`, written out.
//! * `OUTCOME_NAMES` in the graph editor — the menu a person picks from.
//!
//! **Outcomes cross between daemons by name**, so a copy that drifts is not
//! cosmetic: `contracts/INTERACTIONS.md` §5.2 is what it cost once, code 8
//! spelled two ways and a graph from one vocabulary refused by the other.
//!
//! The files are read as text. A reader's one failure mode is a pattern that
//! stops matching and finds nothing, so each reader is also shown text it must
//! not match, and an empty result fails.

use statemachined::model::trial_outcome::declarable;
use statemachined::wire::braemons::v1::TrialOutcome;

fn repository_file(path: &str) -> String {
    let at = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join(path);
    std::fs::read_to_string(&at).unwrap_or_else(|problem| panic!("{}: {problem}", at.display()))
}

/// The text after the line containing `start`, up to the first line starting
/// with `end` — or nothing, which the callers treat as a failure.
fn between<'a>(source: &'a str, start: &str, end: &str) -> Vec<&'a str> {
    let mut lines = source.lines().skip_while(|line| !line.contains(start));
    if lines.next().is_none() {
        return Vec::new();
    }
    lines.take_while(|line| !line.starts_with(end)).collect()
}

/// `NAME = value` pairs, one per line, with anything after the value ignored.
fn assignments(lines: &[&str]) -> Vec<(String, i64)> {
    lines
        .iter()
        .filter_map(|line| {
            let line = line.trim();
            let (name, rest) = line.split_once('=')?;
            let name = name.trim();
            if name.is_empty() || !name.chars().next()?.is_ascii_alphabetic() || name.contains(' ') {
                return None;
            }
            let value: String = rest
                .trim()
                .chars()
                .take_while(|c| *c == '-' || c.is_ascii_digit())
                .collect();
            Some((name.to_string(), value.parse().ok()?))
        })
        .collect()
}

fn taxonomy_from(proto: &str) -> Vec<(String, i64)> {
    assignments(&between(proto, "enum TrialOutcome", "}"))
}

fn firmware_enum_from(header: &str) -> Vec<(String, i64)> {
    assignments(&between(header, "enum class TrialOutcome", "};"))
        .into_iter()
        .map(|(name, value)| (upper_snake(&name), value))
        .collect()
}

fn editor_menu_from(source: &str) -> Vec<String> {
    between(source, "const OUTCOME_NAMES", "];")
        .iter()
        .flat_map(|line| line.split('"').skip(1).step_by(2))
        // Outcome names only: the menu's own blank entry, "no outcome", is not
        // one.
        .filter(|name| {
            name.starts_with(|c: char| c.is_ascii_uppercase())
                && name.chars().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
        })
        .map(str::to_string)
        .collect()
}

fn upper_snake(camel: &str) -> String {
    let mut out = String::new();
    for (at, character) in camel.chars().enumerate() {
        if character.is_ascii_uppercase() && at > 0 {
            out.push('_');
        }
        out.push(character.to_ascii_uppercase());
    }
    out
}

fn taxonomy() -> Vec<(String, i64)> {
    let found = taxonomy_from(&repository_file("proto/braemons/v1/trial_outcome.proto"));
    assert!(!found.is_empty(), "no enum values in trial_outcome.proto this reader can see");
    found
}

#[test]
fn the_readers_find_nothing_where_there_is_nothing() {
    assert!(taxonomy_from("message Other {\n  int32 x = 1;\n}\n").is_empty());
    assert!(firmware_enum_from("enum class Other : int8_t {\n  Hit = 1,\n};\n").is_empty());
    assert!(editor_menu_from("const OTHER = [\n  \"HIT\",\n];\n").is_empty());
    assert_eq!(upper_snake("UnexpectedStartSignal"), "UNEXPECTED_START_SIGNAL");
}

#[test]
fn the_firmware_enum_is_the_taxonomy_value_for_value() {
    let taxonomy = taxonomy();
    let firmware = firmware_enum_from(&repository_file("firmware/core/trial/trial.h"));
    assert!(!firmware.is_empty(), "no values in trial.h this reader can see");
    let mut firmware_sorted = firmware.clone();
    firmware_sorted.sort();
    let mut taxonomy_sorted: Vec<(String, i64)> = taxonomy
        .iter()
        .map(|(name, value)| (name.trim_start_matches("TRIAL_OUTCOME_").to_string(), *value))
        .collect();
    taxonomy_sorted.sort();
    assert_eq!(
        firmware_sorted, taxonomy_sorted,
        "firmware/core/trial/trial.h and proto/braemons/v1/trial_outcome.proto disagree"
    );
}

#[test]
fn the_generated_enum_is_the_taxonomy() {
    // The daemon's own copy is generated, so this only catches a stale
    // `daemon-rs/src/wire/` -- which `make rust-check-proto` also does.
    for (name, value) in taxonomy() {
        let generated = TrialOutcome::from_str_name(&name)
            .unwrap_or_else(|| panic!("{name} is not in the generated enum"));
        assert_eq!(generated as i64, value, "{name}");
    }
}

#[test]
fn what_a_graph_may_declare_is_the_taxonomy_less_the_two_it_may_not() {
    let mut expected: Vec<String> = taxonomy()
        .into_iter()
        .map(|(name, _)| name)
        .filter(|name| name != "UNDETERMINED" && name != "NEVER_FINISHED")
        .collect();
    expected.sort();
    let declarable: Vec<String> = declarable().into_iter().map(str::to_string).collect();
    assert_eq!(
        declarable, expected,
        "model::trial_outcome::declarable does not list every outcome a graph may declare"
    );
}

#[test]
fn the_graph_editor_offers_exactly_what_a_graph_may_declare() {
    let mut offered =
        editor_menu_from(&repository_file("client/web/elements/graph_store_panel_element.js"));
    assert!(!offered.is_empty(), "no OUTCOME_NAMES in the graph editor this reader can see");
    offered.sort();
    let declarable: Vec<String> = declarable().into_iter().map(str::to_string).collect();
    assert_eq!(offered, declarable, "the graph editor's OUTCOME_NAMES");
}
