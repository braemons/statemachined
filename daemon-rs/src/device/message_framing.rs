// SPDX-License-Identifier: AGPL-3.0-or-later
//! Framing: the CRC, the line, and reading one back.
//!
//! This is the **fourth** implementation of the protocol's framing in this
//! repository — after the firmware's, the one under `emulation/tests/`, and the
//! Python daemon's. That is deliberate and it is a cost, and what keeps them
//! honest is that none of them is the authority: `daemon/tests/unit/wire_vectors.json`
//! is, and it is *data*.
//!
//! Every CRC in that file was produced by the emulator's copy and verified
//! against `firmware/core/protocol/crc16.cpp`, so it is golden **against the
//! device** rather than against any host. `tests/framing.rs` runs this
//! implementation over it, which is a stronger check than agreeing with the
//! Python daemon would be: agreeing with the file means agreeing with the board.
//!
//! Replies, unlike commands, go through a real JSON parser — but only after the
//! CRC has said the line arrived intact, and never the other way round. A
//! message whose CRC does not match is not acted on, and printing half of one
//! to a person on a bench is acting on it.

use serde_json::Value;

/// Both rolling checksums in this protocol — the graph upload's and the
/// result's — start here and fold the CRC-covered bytes of each line in order.
pub const CRC_INIT: u16 = 0xFFFF;

const CRC_MEMBER: &str = ",\"crc\":";

/// CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xor-out.
///
/// `seed` is what makes this usable twice: once per line, and once folded
/// across the covered bytes of many lines, which is the rolling checksum
/// `graph_end` and `result_end` carry.
pub fn crc16_ccitt(data: &[u8], seed: u16) -> u16 {
    let mut crc = seed;
    for byte in data {
        crc ^= (*byte as u16) << 8;
        for _ in 0..8 {
            crc = if crc & 0x8000 != 0 {
                (crc << 1) ^ 0x1021
            } else {
                crc << 1
            };
        }
    }
    crc
}

/// Close an object and append its CRC.
///
/// `body` is the message without its closing brace. The CRC covers everything
/// before the `,"crc":` that carries it, which is what lets the device find and
/// check it by arithmetic from the end of the line before parsing a byte.
pub fn statemachined_line(body: &str) -> String {
    format!("{body},\"crc\":\"{:04X}\"}}", crc16_ccitt(body.as_bytes(), CRC_INIT))
}

/// The part of a framed line a rolling checksum folds: everything before `crc`.
///
/// The same span the device's own CRC covers, which is what makes the two
/// checksums comparable at all.
pub fn covered_bytes(line: &str) -> &[u8] {
    let at = line.rfind(CRC_MEMBER).expect("a framed line carries a crc");
    &line.as_bytes()[..at]
}

/// One CRC accumulated across the covered bytes of several lines, in order.
///
/// A message lost or reordered inside an upload is caught by this even though
/// every line that did arrive passed its own check.
pub fn rolling_checksum(lines: &[String], seed: u16) -> u16 {
    lines
        .iter()
        .fold(seed, |crc, line| crc16_ccitt(covered_bytes(line), crc))
}

/// A line came back that is not a message: bad CRC, bad JSON, non-ASCII.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FramingError(pub String);

impl std::fmt::Display for FramingError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for FramingError {}

/// A framed command line, ready for the wire, without its newline.
///
/// Members are written in the order given with `msg_type` and `message_id`
/// first, and the CRC goes on last because **the protocol requires it to be
/// last** — that is what lets the device find it by scanning backwards instead
/// of parsing first.
///
/// `fields` is a slice of pairs rather than a map, because the order is part of
/// the bytes: a map that sorted them would produce a different line with a
/// different CRC, which the golden vectors would catch and a rig would not.
pub fn command_line(msg_type: &str, message_id: u64, fields: &[(&str, Value)]) -> String {
    let mut body = format!("{{\"msg_type\":\"{msg_type}\",\"message_id\":{message_id}");
    for (key, value) in fields {
        // `null` is written, not dropped: this protocol distinguishes the two.
        // `terminal` must be present on every graph_state, as an outcome code
        // or as null for a state that is not terminal, and a device that
        // silently accepted the member's absence would be guessing which a
        // graph meant. Omit a field by not passing it.
        body.push_str(&format!(",\"{key}\":{}", compact(value)));
    }
    statemachined_line(&body)
}

/// One value, with no spaces — what `json.dumps(separators=(",", ":"))` gives.
fn compact(value: &Value) -> String {
    serde_json::to_string(value).expect("a Value serialises")
}

/// Check a received line's CRC, then parse it. **In that order.**
pub fn parse_reply(line: &str) -> Result<Value, FramingError> {
    let line = line.trim();
    if !line.is_ascii() {
        return Err(FramingError(
            "line contains a byte >= 0x80, which is a framing error".into(),
        ));
    }
    let marker = ",\"crc\":\"";
    let Some(at) = line.rfind(marker) else {
        return Err(FramingError(format!("no trailing crc member: {line:?}")));
    };
    if !line.ends_with("\"}") {
        return Err(FramingError(format!("no trailing crc member: {line:?}")));
    }
    let claimed = &line[at + marker.len()..line.len() - 2];
    let actual = format!("{:04X}", crc16_ccitt(&line.as_bytes()[..at], CRC_INIT));
    if claimed.to_ascii_uppercase() != actual {
        return Err(FramingError(format!(
            "crc mismatch: line says {claimed}, bytes say {actual}"
        )));
    }
    serde_json::from_str(line)
        .map_err(|problem| FramingError(format!("crc was good but the line is not JSON: {problem}")))
}

/// The device refused a command, which is a normal outcome and not a bug.
///
/// Every refusal names what to change, so the `context` is the useful half and
/// is never dropped.
#[derive(Debug, Clone)]
pub struct DeviceRefusedTheCommand {
    pub code: String,
    pub message: String,
    pub context: String,
}

impl DeviceRefusedTheCommand {
    pub fn from_reply(reply: &Value) -> Self {
        let text = |key: &str| {
            reply
                .get(key)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string()
        };
        Self {
            code: reply
                .get("code")
                .and_then(Value::as_str)
                .unwrap_or("?")
                .to_string(),
            message: text("message"),
            context: text("context"),
        }
    }
}

impl std::fmt::Display for DeviceRefusedTheCommand {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let detail: Vec<&str> = [self.message.as_str(), self.context.as_str()]
            .into_iter()
            .filter(|part| !part.is_empty())
            .collect();
        if detail.is_empty() {
            f.write_str(&self.code)
        } else {
            write!(f, "{} ({})", self.code, detail.join(": "))
        }
    }
}

impl std::error::Error for DeviceRefusedTheCommand {}
