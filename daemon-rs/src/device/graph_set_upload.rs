// SPDX-License-Identifier: AGPL-3.0-or-later
//! A graph set, sent message by message, with the rolling checksum kept.
//!
//! A **set**, not a graph: a session uploads every graph it will use before its
//! first trial and then switches between them with `configure`'s
//! `graph_index`. See docs/reference/protocol.md §3.2.
//!
//! The checksum is the point. `set_end` carries a CRC-16 over the covered bytes
//! of every upload message before it (PROTOCOL.md §3.2), so a chunk that went
//! missing is caught even though the line that vanished was perfectly well
//! formed — and computing it from the bytes this side actually sent is the only
//! way to tell that the device folded the same ones.
//!
//! **Only the daemon's half is ported.** Python's `GraphSetUploader` and
//! `SingleGraphSetUploader` build a set by hand for the hardware suite, which
//! stays in Python and imports them from `daemon/`; nothing in the daemon calls
//! them.

use std::time::Duration;

use serde_json::{Map, Value};

use crate::device::message_framing::{command_line, covered_bytes, crc16_ccitt, CRC_INIT};
use crate::device::message_vocabulary::MsgType;
use crate::device::request_response_session::{RequestProblem, RequestResponseSession, Sink};
use crate::graph_set_compiler::UploadMessage;

/// Put a compiled set on the wire and return the `set_ok`.
///
/// `upload_messages` is what the compiler produced: `set_begin` to `set_end`,
/// with `set_end`'s `checksum` left out because it is over bytes that did not
/// exist yet.
///
/// This is the seam. The compiler knows what a name means and nothing about
/// framing; this knows the framing and nothing about names. The checksum is the
/// reason the two have to meet somewhere: it is folded over the CRC-covered
/// bytes of every message actually sent, so it can only be computed by whoever
/// sent them.
pub fn send_compiled_upload_messages<S: Sink>(
    session: &mut RequestResponseSession<S>,
    upload_messages: &[UploadMessage],
    timeout: Duration,
) -> Result<Value, RequestProblem> {
    let mut rolling_checksum = CRC_INIT;
    let mut reply = Value::Object(Map::new());
    for upload_message in upload_messages {
        let mut fields = upload_message.fields.clone();
        let is_set_end = upload_message.msg_type == MsgType::SetEnd;
        if is_set_end {
            fields.insert(
                "checksum".into(),
                Value::String(format!("{rolling_checksum:04X}")),
            );
        }

        let message_id = session.next_message_id();
        let pairs: Vec<(&str, Value)> = fields
            .iter()
            .map(|(key, value)| (key.as_str(), value.clone()))
            .collect();
        let line = command_line(upload_message.msg_type.as_str(), message_id as u64, &pairs);
        if !is_set_end {
            // set_end carries the checksum and so cannot be inside it.
            rolling_checksum = crc16_ccitt(covered_bytes(&line), rolling_checksum);
        }
        reply =
            session.request_line(&line, message_id, timeout, upload_message.msg_type.as_str())?;
    }
    Ok(reply)
}
