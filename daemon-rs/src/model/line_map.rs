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
use crate::device::device_pin_map::{Direction, DevicePinMap};

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

    /// The body of the protocol's `wiring` command, as this rig needs it.
    ///
    /// **Every field, always**, rather than only what differs from the default:
    /// the command replaces what the board holds, and a partial one would leave
    /// a board that had been moved between rigs carrying half of each.
    ///
    /// Takes a **resolved** map. A map whose indices came from pin labels is
    /// only numbered once `resolved_against` has asked the board, and masks are
    /// the last place to discover that — so an unresolved line is refused here
    /// rather than silently treated as line 0.
    pub fn wiring_message_fields(&self) -> Checked<WiringFields> {
        let mut invert_mask: u32 = 0;
        let mut enable_mask: u32 = 0;
        let mut debounce_milliseconds_per_line = vec![0i64; MAXIMUM_LINE_COUNT as usize];

        for line in &self.input_lines {
            let line_index = numbered(line.line_index, &line.name, &line.pin_label, "input")?;
            if line.reads_active_low {
                invert_mask |= 1 << line_index;
            }
            if line.is_enabled {
                enable_mask |= 1 << line_index;
            }
            debounce_milliseconds_per_line[line_index as usize] = line.debounce_milliseconds;
        }

        let mut safe_level_mask: u32 = 0;
        for line in &self.output_lines {
            let line_index = numbered(line.line_index, &line.name, &line.pin_label, "output")?;
            safe_level_mask |= u32::from(line.safe_level_is_high) << line_index;
        }

        // Trailing zeros are dropped because the device fills the rest with
        // zeros anyway, and a 32-entry array of nothing is most of a protocol
        // line's budget.
        while debounce_milliseconds_per_line.last() == Some(&0) {
            debounce_milliseconds_per_line.pop();
        }

        Ok(WiringFields {
            invert: invert_mask,
            enable: enable_mask,
            safe: safe_level_mask,
            debounce_ms: debounce_milliseconds_per_line,
        })
    }

    pub fn input_line_index_for_name(&self, name: &str) -> Checked<i64> {
        if let Some(line) = self.input_lines.iter().find(|line| line.name == name) {
            return numbered(line.line_index, &line.name, &line.pin_label, "input");
        }
        Err(no_line_called(name, "input", self.input_lines.iter().map(|l| l.name.as_str())))
    }

    pub fn output_line_index_for_name(&self, name: &str) -> Checked<i64> {
        if let Some(line) = self.output_lines.iter().find(|line| line.name == name) {
            return numbered(line.line_index, &line.name, &line.pin_label, "output");
        }
        Err(no_line_called(name, "output", self.output_lines.iter().map(|l| l.name.as_str())))
    }
}

/// "No such line", listing the ones there are, alphabetically so the sentence
/// is the same whatever order the config was written in.
fn no_line_called<'a>(name: &str, which: &str, known: impl Iterator<Item = &'a str>) -> Refused {
    let mut known: Vec<&str> = known.collect();
    known.sort_unstable();
    Refused(format!(
        "no {which} line is called '{name}'. This rig has: {}",
        if known.is_empty() { "(none)".to_string() } else { known.join(", ") }
    ))
}

/// A map this board cannot honour: a pin it has not got, or a `line_index` and
/// a `pin_label` that disagree.
///
/// **A kind of its own, not a bare refusal**, because it reaches a caller as a
/// refusal and the refusal table has to tell it from "this file does not
/// parse". It was not, once, and every `LoadConfig` that hit it answered
/// `internal` — which tells the person holding the config that the daemon
/// broke, when what happened is that their config names a pin this board has
/// not got.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LineMapDoesNotMatchTheBoard(pub String);

impl std::fmt::Display for LineMapDoesNotMatchTheBoard {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for LineMapDoesNotMatchTheBoard {}

/// The `wiring` command's body.
///
/// Serialised in this order, which is the order the protocol document writes
/// them in — not that the device cares, but a line in a monitor is read by a
/// person holding that document.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WiringFields {
    pub invert: u32,
    pub enable: u32,
    pub safe: u32,
    pub debounce_ms: Vec<i64>,
}

/// A line's index, or a refusal to guess one.
///
/// A line may be configured by pin alone, and until `resolved_against` has asked
/// the board there is no index. Everything below this deals in masks, so this is
/// the boundary where "not yet resolved" has to stop being representable.
fn numbered(
    line_index: Option<i64>,
    name: &str,
    pin_label: &str,
    where_: &str,
) -> Checked<i64> {
    line_index.ok_or_else(|| {
        Refused(format!(
            "the {where_} line '{name}' is configured by pin ('{pin_label}') and has not been \
             resolved against a board yet"
        ))
    })
}

/// One line, as the resolution sees it, whichever direction it came from.
struct Unresolved<'a> {
    name: &'a str,
    line_index: Option<i64>,
    pin_label: &'a str,
}

impl LineMap {
    /// This map with every `line_index` filled in and checked against the board.
    ///
    /// The board answers with the same table `pinMode()` was called over, so a
    /// pin label is no longer a comment. Three things happen here, and they are
    /// the whole point of the command existing:
    ///
    /// * a line naming only a **pin** gets its index from the board, so the
    ///   config says the thing a person can check against the hardware in front
    ///   of them rather than a bit position nothing is labelled with;
    /// * a line naming **both** has them checked, and a disagreement is
    ///   refused. That is the silent wrong-valve bug: `pin_label = "A0"` on line
    ///   4 looks right in every listing and drives A1;
    /// * a line naming a **pin this board has not got** is refused, which is the
    ///   typo that used to survive as far as an animal in the booth.
    ///
    /// **Only where the board actually answered.** A pin map this daemon
    /// assumed is advisory: it fills in a missing index, and it never overrules
    /// or refuses one that was written down. Refusing on a hand-copied table
    /// would be asserting the very thing this exists to stop asserting.
    ///
    /// The caller closes the link on a refusal: masks built from a wrong index
    /// are not something to push and then warn about.
    pub fn resolved_against(
        &self,
        pin_map: &DevicePinMap,
    ) -> Result<LineMap, LineMapDoesNotMatchTheBoard> {
        let mut resolved = LineMap::default();
        for line in &self.input_lines {
            let mut line = line.clone();
            line.line_index = Some(resolve_one(
                &Unresolved {
                    name: &line.name,
                    line_index: line.line_index,
                    pin_label: &line.pin_label,
                },
                Direction::In,
                pin_map,
            )?);
            resolved.input_lines.push(line);
        }
        for line in &self.output_lines {
            let mut line = line.clone();
            line.line_index = Some(resolve_one(
                &Unresolved {
                    name: &line.name,
                    line_index: line.line_index,
                    pin_label: &line.pin_label,
                },
                Direction::Out,
                pin_map,
            )?);
            resolved.output_lines.push(line);
        }
        Ok(resolved)
    }
}

/// One line's index: what the board says, what the config says, or a refusal.
fn resolve_one(
    line: &Unresolved,
    direction: Direction,
    pin_map: &DevicePinMap,
) -> Result<i64, LineMapDoesNotMatchTheBoard> {
    let refuse = |sentence: String| Err(LineMapDoesNotMatchTheBoard(sentence));
    let where_ = direction.spelled();
    let from_the_board = if line.pin_label.is_empty() {
        None
    } else {
        pin_map.line_index_for_label(direction, line.pin_label)
    };

    if !pin_map.came_from_the_device() {
        // An assumed map may fill a gap and may not contradict anybody.
        if let Some(index) = line.line_index {
            return Ok(index);
        }
        if let Some(index) = from_the_board {
            return Ok(index);
        }
        return refuse(format!(
            "the {where_} line '{}' names pin '{}', and this board did not say which pins it \
             has -- its firmware is older than the `pins` command. Give it a line_index, or \
             flash firmware that answers `pins`",
            line.name, line.pin_label
        ));
    }

    if !line.pin_label.is_empty() && from_the_board.is_none() {
        return refuse(format!(
            "the {where_} line '{}' names pin '{}', which is not an {} on this board. It has: {}",
            line.name,
            line.pin_label,
            where_,
            pin_map.known_pins(direction)
        ));
    }

    let Some(line_index) = line.line_index else {
        return Ok(from_the_board.expect("a line with neither is refused when it is validated"));
    };

    if let Some(from_the_board) = from_the_board {
        if from_the_board != line_index {
            return refuse(format!(
                "the {where_} line '{}' says line {line_index} and pin '{}', but this board's \
                 {where_} line {line_index} is pin '{}' and '{}' is line {from_the_board}. One \
                 of the two is wrong, and nothing downstream would notice which",
                line.name,
                line.pin_label,
                pin_map.label_for(direction, line_index),
                line.pin_label
            ));
        }
    }

    if line_index >= pin_map.line_count(direction) as i64 {
        return refuse(format!(
            "the {where_} line '{}' is line {line_index}, and this board has no such {where_} \
             line. Its pins are: {}",
            line.name,
            pin_map.known_pins(direction)
        ));
    }
    Ok(line_index)
}
