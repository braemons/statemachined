// SPDX-License-Identifier: AGPL-3.0-or-later
//! What the board says its pins are called, and **where that answer came from**.
//!
//! Which pin a line is, and which direction it has, are fixed when the firmware
//! is compiled — the HAL's tables are what `pinMode()` is called over, and no
//! command changes either. So a host cannot derive this; it can only ask, or
//! assume.
//!
//! **Both are represented here, and they are not the same thing.** A map whose
//! source is `Device` was read off the board this daemon is talking to. One
//! whose source is `Assumed` came from a table in the host, keyed by board name
//! — a hand-copied pin map, which is exactly what the `pins` command exists to
//! replace. Firmware flashed before that command existed answers `no_pin_map`,
//! which **is not a failure**: a rig in a rack is reflashed when somebody gets
//! to it.
//!
//! What the difference is *for* is honesty in the API and the UI. A pin label
//! the board vouched for can be shown as fact; one the daemon assumed has to be
//! shown as an assumption, because the failure it hides — a valve driven from a
//! lever's line number — is silent everywhere else.

use serde::{Deserialize, Serialize};

/// Which numbering a label belongs to.
///
/// **Not decoration.** Input line 3 and output line 3 are different pins over
/// different numberings, so a label is only meaningful alongside its direction.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    In,
    Out,
}

impl Direction {
    /// How a refusal names it.
    pub fn spelled(&self) -> &'static str {
        match self {
            Self::In => "input",
            Self::Out => "output",
        }
    }
}

/// Where a pin label came from.
///
/// `Unknown` is a board with no entry in either place: the next MCU, on the far
/// end of an ethernet cable.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PinLabelSource {
    Device,
    Assumed,
    #[default]
    Unknown,
}

/// One board's two line numberings, by label.
///
/// **Two lists, not one map.** A single mapping from label to index would
/// collapse input 3 and output 3, which is a lever's number driving a valve.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct DevicePinMap {
    #[serde(default)]
    pub input_pin_labels: Vec<String>,
    #[serde(default)]
    pub output_pin_labels: Vec<String>,
    #[serde(default)]
    pub source: PinLabelSource,
}

impl DevicePinMap {
    pub fn came_from_the_device(&self) -> bool {
        self.source == PinLabelSource::Device
    }

    fn labels(&self, direction: Direction) -> &[String] {
        match direction {
            Direction::In => &self.input_pin_labels,
            Direction::Out => &self.output_pin_labels,
        }
    }

    /// The label of one line, or `""` where this map does not reach it.
    pub fn label_for(&self, direction: Direction, line_index: i64) -> &str {
        usize::try_from(line_index)
            .ok()
            .and_then(|at| self.labels(direction).get(at))
            .map(String::as_str)
            .unwrap_or_default()
    }

    /// Which line that pin is, or `None` if this board has no such pin.
    ///
    /// Case-insensitive, because `a0` and `A0` are the same hole in the board
    /// and refusing a config over the difference would be pedantry with a
    /// soldering iron in the room.
    pub fn line_index_for_label(&self, direction: Direction, pin_label: &str) -> Option<i64> {
        let wanted = pin_label.trim().to_lowercase();
        self.labels(direction)
            .iter()
            .position(|label| label.to_lowercase() == wanted)
            .map(|at| at as i64)
    }

    pub fn known_pins(&self, direction: Direction) -> String {
        let labels = self.labels(direction);
        if labels.is_empty() {
            "(none)".to_string()
        } else {
            labels.join(", ")
        }
    }

    pub fn line_count(&self, direction: Direction) -> usize {
        self.labels(direction).len()
    }
}
