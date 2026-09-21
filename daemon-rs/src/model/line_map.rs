// SPDX-License-Identifier: AGPL-3.0-or-later
//! What each line is called, and what the rig did to it.
//!
//! Two different things live here and the difference is load-bearing.
//!
//! **Names are the daemon's alone and never reach the wire.** `lever_left` is
//! line 4 to the device and nothing else; renaming it is free, changes no
//! graph, and needs no upload. That is what lets a graph be authored against
//! words rather than against a pinout somebody has to remember.
//!
//! **The wiring does reach the wire**, as the protocol's `wiring` command:
//! invert, enable, debounce and the output safe levels. It describes the box
//! rather than the paradigm, so it lives on the line map, is sent once when a
//! rig is wired, and is not part of any graph.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use super::graph_definition::{Refused, Result as Checked};

/// One `uint32_t` of input word on the reference board.
///
/// Widening it is a type change reaching every struct in the firmware and the
/// wire format, so a line map that asks for more is refused here rather than
/// truncated there.
pub const MAXIMUM_LINE_COUNT: i64 = 32;

/// One input line: what it is called, and how the rig conditions it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InputLineDefinition {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub line_index: Option<i64>,
    #[serde(default)]
    pub reads_active_low: bool,
    #[serde(default = "yes")]
    pub is_enabled: bool,
    #[serde(default)]
    pub debounce_milliseconds: i64,
    /// The pin as the board calls it — `A0`. Resolved against the board the
    /// moment a config is loaded, which is what lets a config move between rigs
    /// and be refused loudly rather than drive the wrong line silently.
    #[serde(default)]
    pub pin_label: String,
}

/// One output line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputLineDefinition {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub line_index: Option<i64>,
    /// What this line rests at when nothing is driving it.
    #[serde(default)]
    pub safe_level_is_high: bool,
    #[serde(default)]
    pub pin_label: String,
}

fn yes() -> bool {
    true
}

/// Every line this rig has, by name.
///
/// The one object that knows both halves: which word a name means, and what the
/// device must be told about the wiring behind it.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LineMap {
    #[serde(default)]
    pub input_lines: Vec<InputLineDefinition>,
    #[serde(default)]
    pub output_lines: Vec<OutputLineDefinition>,
}

/// One line, whichever direction, for the checks both share.
struct Line<'a> {
    name: &'a str,
    line_index: Option<i64>,
    pin_label: &'a str,
}

impl LineMap {
    pub fn validate(&self) -> Checked<()> {
        let inputs: Vec<Line> = self
            .input_lines
            .iter()
            .map(|line| Line {
                name: &line.name,
                line_index: line.line_index,
                pin_label: &line.pin_label,
            })
            .collect();
        let outputs: Vec<Line> = self
            .output_lines
            .iter()
            .map(|line| Line {
                name: &line.name,
                line_index: line.line_index,
                pin_label: &line.pin_label,
            })
            .collect();

        for line in &self.input_lines {
            if line.debounce_milliseconds < 0 || line.debounce_milliseconds > 65535 {
                return Err(Refused(format!(
                    "the input line '{}' debounces for {} ms",
                    line.name, line.debounce_milliseconds
                )));
            }
        }

        for (kind, lines) in [("input", &inputs), ("output", &outputs)] {
            let mut seen_names: BTreeSet<&str> = BTreeSet::new();
            let mut seen_indices: BTreeSet<i64> = BTreeSet::new();
            for line in lines.iter() {
                if line.name.is_empty() {
                    return Err(Refused(format!("an {kind} line has no name")));
                }
                if !seen_names.insert(line.name) {
                    return Err(Refused(format!(
                        "two {kind} lines are called '{}'",
                        line.name
                    )));
                }
                if line.line_index.is_none() && line.pin_label.is_empty() {
                    return Err(Refused(format!(
                        "the {kind} line '{}' says neither which line it is nor which pin: give \
                         it a line_index, or a pin_label the board knows",
                        line.name
                    )));
                }
                if let Some(index) = line.line_index {
                    if !(0..MAXIMUM_LINE_COUNT).contains(&index) {
                        return Err(Refused(format!(
                            "the {kind} line '{}' is line {index}, and a board has {} lines",
                            line.name, MAXIMUM_LINE_COUNT
                        )));
                    }
                    // Two names for one line is not a harmless alias: a graph
                    // naming both would raise one line and believe it had
                    // raised two, and the mistake is invisible in the record.
                    if !seen_indices.insert(index) {
                        return Err(Refused(format!(
                            "two {kind} lines are line {index}: '{}' is a second name for it",
                            line.name
                        )));
                    }
                }
            }
        }
        Ok(())
    }

    pub fn input_line_index_for_name(&self, name: &str) -> Checked<i64> {
        self.input_lines
            .iter()
            .find(|line| line.name == name)
            .and_then(|line| line.line_index)
            .ok_or_else(|| {
                let known: Vec<&str> =
                    self.input_lines.iter().map(|l| l.name.as_str()).collect();
                Refused(format!(
                    "no input line is called '{name}'. This rig has: {}",
                    if known.is_empty() { "(none)".to_string() } else { known.join(", ") }
                ))
            })
    }

    pub fn output_line_index_for_name(&self, name: &str) -> Checked<i64> {
        self.output_lines
            .iter()
            .find(|line| line.name == name)
            .and_then(|line| line.line_index)
            .ok_or_else(|| {
                let known: Vec<&str> =
                    self.output_lines.iter().map(|l| l.name.as_str()).collect();
                Refused(format!(
                    "no output line is called '{name}'. This rig has: {}",
                    if known.is_empty() { "(none)".to_string() } else { known.join(", ") }
                ))
            })
    }
}
