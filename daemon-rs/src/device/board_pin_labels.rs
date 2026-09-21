// SPDX-License-Identifier: AGPL-3.0-or-later
//! Line number to pin label, **for printing only**.
//!
//! The map itself is a wire contract and lives in the hardware document; this
//! table is a convenience so that a line prints as something a person can hold
//! against the wires in front of them. It is keyed by the `board` string in
//! `hello_ack`, and **a board that is not in it prints bare line numbers rather
//! than a plausible lie about somebody else's pinout** — which is the same
//! reason `DevicePinMap` records where its labels came from.

use crate::device::device_pin_map::Direction;

/// The reference board. D0/D1 are the UART and D13 is the on-board LED, so
/// neither is a line.
const UNO_R4_MINIMA_IN: [&str; 8] = ["D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9"];
const UNO_R4_MINIMA_OUT: [&str; 8] = ["D10", "D11", "D12", "A0", "A1", "A2", "A3", "A4"];

fn pins_of(board: &str, direction: Direction) -> &'static [&'static str] {
    match (board, direction) {
        ("uno_r4_minima", Direction::In) => &UNO_R4_MINIMA_IN,
        ("uno_r4_minima", Direction::Out) => &UNO_R4_MINIMA_OUT,
        _ => &[],
    }
}

/// `"3 (A0)"` where the pinout is known, `"3"` where it is not.
pub fn pin_label(board: &str, direction: Direction, line: i64) -> String {
    let pins = pins_of(board, direction);
    match usize::try_from(line).ok().and_then(|at| pins.get(at)) {
        Some(pin) => format!("{line} ({pin})"),
        None => line.to_string(),
    }
}

/// A line mask as a row of characters, **line 0 on the left**.
///
/// The opposite of how a binary literal reads, and the same as how the hardware
/// table does. Anybody comparing this against a breadboard is counting from
/// line 0.
pub fn word_bits(word: u32, count: u32) -> String {
    (0..count)
        .map(|bit| if word & (1 << bit) != 0 { '1' } else { '.' })
        .collect()
}

/// The lines a mask has high, named.
pub fn high_lines(board: &str, direction: Direction, word: u32, count: u32) -> String {
    let named: Vec<String> = (0..count)
        .filter(|bit| word & (1 << bit) != 0)
        .map(|bit| pin_label(board, direction, bit as i64))
        .collect();
    if named.is_empty() {
        "none".to_string()
    } else {
        named.join(", ")
    }
}
