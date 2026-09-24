// SPDX-License-Identifier: AGPL-3.0-or-later
//! Every line in and out of the port, as it went, kept for a while.
//!
//! The thing a person reaches for when the layers above have stopped agreeing:
//! `ReadLines` says the valve is line 3 and the valve is not opening, so what
//! actually went down the wire? This answers that, in the protocol's own words,
//! with nothing interpreting them.
//!
//! **Not the trace.** The trace is the *record*, written to disk and joined to
//! triald's `.tdr`. This is a *log*: lines, in order, thrown away when the ring
//! wraps. **It is always on**, because a link fault that happens once an hour
//! is not reproducible on demand, and a monitor somebody has to enable first is
//! off when the interesting thing happens.

use std::collections::VecDeque;
use std::sync::Mutex;

use serde_json::{json, Value};

use super::device_clock_correlation::iso8601_utc;

/// How many lines the ring holds: minutes of an idle link, or one whole graph
/// set upload with room around it — the two things anybody scrolls back to.
pub const MONITORED_LINE_COUNT: usize = 4000;

struct Held {
    lines: VecDeque<Value>,
    /// Monotonic for the life of the daemon: a position in a ring is not a
    /// position in a log.
    next_entry_number: i64,
}

/// The last few thousand lines, both directions, with their arrival times.
pub struct DeviceLineMonitor {
    held: Mutex<Held>,
    ring_capacity: usize,
}

impl Default for DeviceLineMonitor {
    fn default() -> Self {
        Self::new(MONITORED_LINE_COUNT)
    }
}

impl DeviceLineMonitor {
    pub fn new(ring_lines: usize) -> Self {
        Self {
            held: Mutex::new(Held {
                lines: VecDeque::new(),
                next_entry_number: 0,
            }),
            ring_capacity: ring_lines,
        }
    }

    fn held(&self) -> std::sync::MutexGuard<'_, Held> {
        self.held
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub fn ring_capacity(&self) -> usize {
        self.ring_capacity
    }

    /// One line, as it went. **Never fails**: this is in the path of every
    /// command and of the visit stream, and a monitor that could break the
    /// link would be worse than none.
    pub fn record(&self, direction: &str, line: &str) {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|since| since.as_secs_f64())
            .unwrap_or(0.0);
        let mut held = self.held();
        let entry = json!({
            "entry_number": held.next_entry_number,
            "direction": direction,
            "line": line,
            "recorded_host_time": iso8601_utc(now),
        });
        held.next_entry_number += 1;
        if self.ring_capacity > 0 {
            if held.lines.len() == self.ring_capacity {
                held.lines.pop_front();
            }
            held.lines.push_back(entry);
        }
    }

    pub fn lines_since(&self, entry_number: i64, limit: usize) -> Vec<Value> {
        self.held()
            .lines
            .iter()
            .filter(|line| number_of(line) >= entry_number)
            .take(limit)
            .cloned()
            .collect()
    }

    pub fn newest_entry_number(&self) -> i64 {
        self.held().next_entry_number - 1
    }

    pub fn oldest_entry_number_still_held(&self) -> i64 {
        let held = self.held();
        held.lines
            .front()
            .map(number_of)
            .unwrap_or(held.next_entry_number)
    }

    /// Whether a cursor is older than anything still held: a consumer that
    /// fell behind is told what it missed, not handed a shorter answer.
    pub fn has_fallen_out_of_the_ring(&self, entry_number: i64) -> bool {
        self.held()
            .lines
            .front()
            .is_some_and(|oldest| entry_number < number_of(oldest))
    }

    pub fn len(&self) -> usize {
        self.held().lines.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

fn number_of(line: &Value) -> i64 {
    line.get("entry_number")
        .and_then(Value::as_i64)
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lines_are_numbered_by_the_daemon_and_kept_in_both_directions() {
        let monitor = DeviceLineMonitor::new(10);
        monitor.record("to_device", "a");
        monitor.record("from_device", "b");
        let lines = monitor.lines_since(0, 500);
        assert_eq!(lines[0]["direction"], "to_device");
        assert_eq!(lines[1]["entry_number"], 1);
        assert_eq!(lines[1]["line"], "b");
    }

    #[test]
    fn the_ring_drops_the_oldest_and_a_cursor_behind_it_is_told_so() {
        let monitor = DeviceLineMonitor::new(3);
        for index in 0..5 {
            monitor.record("from_device", &index.to_string());
        }
        assert_eq!(monitor.len(), 3);
        assert_eq!(monitor.oldest_entry_number_still_held(), 2);
        assert_eq!(monitor.newest_entry_number(), 4);
        assert!(monitor.has_fallen_out_of_the_ring(1));
        assert!(!monitor.has_fallen_out_of_the_ring(2));
    }

    #[test]
    fn an_empty_monitor_has_lost_nothing() {
        let monitor = DeviceLineMonitor::new(3);
        assert!(!monitor.has_fallen_out_of_the_ring(0));
        assert_eq!(monitor.oldest_entry_number_still_held(), 0);
        assert_eq!(monitor.newest_entry_number(), -1);
    }
}
