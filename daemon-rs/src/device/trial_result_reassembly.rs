// SPDX-License-Identifier: AGPL-3.0-or-later
//! Reassembling `result_begin` … `result_path`* … `result_end`.
//!
//! Lines are folded here rather than parsed and dropped, for the checksum: the
//! device folds the covered bytes of every result line before `result_end`, so
//! the host has to fold the same bytes, and a parsed object has already thrown
//! them away.

use serde_json::Value;

use super::message_framing::{covered_bytes, crc16_ccitt, CRC_INIT};
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
            .and_then(Value::as_str)
            .is_some_and(|claimed| claimed.to_ascii_uppercase() == format!("{:04X}", self.computed))
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

    /// Take one already-parsed line. The result, when this line completes it.
    pub fn feed(
        &mut self,
        line: &str,
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
                self.rolling_checksum = crc16_ccitt(covered_bytes(line), CRC_INIT);
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
                self.rolling_checksum = crc16_ccitt(covered_bytes(line), self.rolling_checksum);
                if let Some(rows) = message.get("p").and_then(Value::as_array) {
                    self.rows.extend(rows.iter().cloned());
                }
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
