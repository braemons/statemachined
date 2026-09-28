// SPDX-License-Identifier: AGPL-3.0-or-later
//! Reassembling `result_begin` … `result_path`* … `result_end`.
//!
//! Lines are folded here rather than parsed and dropped, for the checksum: the
//! device folds the covered bytes of every result line before `result_end`, so
//! the host has to fold the same bytes, and a parsed object has already thrown
//! them away.

use serde_json::Value;

use super::message_framing::{crc16_ccitt, CRC_INIT};
use super::message_vocabulary::{field, MsgType};

/// A trial's result, reassembled, with the checksum this side computed.
#[derive(Debug, Clone, PartialEq)]
pub struct ReassembledTrialResult {
    pub begin: Value,
    pub rows: Vec<Value>,
    pub end: Value,
    pub computed: u16,
}

impl ReassembledTrialResult {
    pub fn checksum_matches(&self) -> bool {
        self.end
            .get("checksum")
            .and_then(Value::as_u64)
            .is_some_and(|claimed| claimed == self.computed as u64)
    }
}

/// Fed one line at a time, and finished when the last chunk arrives.
///
/// A collector rather than a loop, because a daemon does not get to sit in
/// one: a result arrives unasked, in the middle of whatever else the link is
/// doing. Ordering is checked as strictly as the protocol states it — a chunk
/// that overtook its `result_begin` would break the fold silently.
#[derive(Debug, Default)]
pub struct TrialResultCollector {
    begin: Option<Value>,
    rows: Vec<Value>,
    rolling_checksum: u16,
}

impl TrialResultCollector {
    pub fn new() -> Self {
        Self {
            begin: None,
            rows: Vec::new(),
            rolling_checksum: CRC_INIT,
        }
    }

    pub fn is_assembling(&self) -> bool {
        self.begin.is_some()
    }

    /// Take one decoded message, with the protobuf it arrived as. The result,
    /// when this message completes it.
    pub fn feed(
        &mut self,
        payload: &[u8],
        message: &Value,
    ) -> Result<Option<ReassembledTrialResult>, String> {
        let kind = message
            .get(field::MSG_TYPE)
            .and_then(Value::as_str)
            .and_then(MsgType::named);
        match kind {
            Some(MsgType::ResultBegin) => {
                self.begin = Some(message.clone());
                self.rows.clear();
                self.rolling_checksum = crc16_ccitt(payload, CRC_INIT);
                Ok(None)
            }
            Some(MsgType::ResultPath) => {
                if self.begin.is_none() {
                    return Err("result_path arrived before result_begin".into());
                }
                let from = message.get("from").and_then(Value::as_i64).unwrap_or(-1);
                if from != self.rows.len() as i64 {
                    return Err(format!(
                        "result_path starts at {from}, but {} rows have arrived: a chunk is \
                         missing or out of order",
                        self.rows.len()
                    ));
                }
                self.rolling_checksum = crc16_ccitt(payload, self.rolling_checksum);
                self.rows.extend(rows_of(message)?);
                Ok(None)
            }
            Some(MsgType::ResultEnd) => {
                let Some(begin) = self.begin.take() else {
                    return Err("result_end arrived before result_begin".into());
                };
                // Deliberately not folded: it carries the checksum.
                Ok(Some(ReassembledTrialResult {
                    begin,
                    rows: std::mem::take(&mut self.rows),
                    end: message.clone(),
                    computed: self.rolling_checksum,
                }))
            }
            _ => Ok(None),
        }
    }
}

/// The fields of a path row, in the order a `result_path` carries them as
/// parallel arrays (link.proto) and under the names a `visit` carries them.
const ROW_FIELDS: [&str; 6] = [
    "state",
    "exit",
    "transition",
    "drawn_ms",
    "entered_us",
    "duration_us",
];

/// A `result_path`'s rows, zipped back out of its arrays into the objects a
/// `visit` is, so one decoder reads both. Arrays of different lengths are a
/// chunk that cannot be read, not rows to guess at.
fn rows_of(message: &Value) -> Result<Vec<Value>, String> {
    let empty = Vec::new();
    let columns: Vec<&Vec<Value>> = ROW_FIELDS
        .iter()
        .map(|name| message.get(*name).and_then(Value::as_array).unwrap_or(&empty))
        .collect();
    let n = columns[0].len();
    if columns.iter().any(|column| column.len() != n) {
        return Err(format!(
            "a result_path's arrays differ in length: {}",
            ROW_FIELDS
                .iter()
                .zip(&columns)
                .map(|(name, column)| format!("{name} {}", column.len()))
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    Ok((0..n)
        .map(|i| {
            Value::Object(
                ROW_FIELDS
                    .iter()
                    .zip(&columns)
                    .map(|(name, column)| (name.to_string(), column[i].clone()))
                    .collect(),
            )
        })
        .collect())
}
