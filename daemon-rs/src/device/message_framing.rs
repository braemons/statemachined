// SPDX-License-Identifier: AGPL-3.0-or-later
//! Framing: COBS, the CRC, and a frame read back.
//!
//! Both directions, identically:
//!
//! ```text
//! COBS( protobuf message ‖ CRC-16, big-endian ) ‖ 0x00
//! ```
//!
//! The same frame the firmware's `core/protocol/framing.cpp` builds and
//! mousewheeld's link uses. A frame is checked whole — delimited, COBS, CRC —
//! before a byte of it is decoded, and never the other way round: a message
//! whose CRC does not match is not acted on, and printing half of one to a
//! person on a bench is acting on it.

use serde_json::Value;

/// Both rolling checksums in this protocol — the set upload's and the
/// result's — start here and fold the protobuf of each message in order.
pub const CRC_INIT: u16 = 0xFFFF;

/// The frame budget the board reports as `caps.max_frame`, and what a reader
/// here gives up at when a delimiter never comes. The board's own limit is
/// what a sender has to respect; this only stops a stream of junk from growing
/// a buffer for ever.
pub const MAX_FRAME: usize = 4096;

/// CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xor-out.
///
/// `seed` is what makes this usable twice: once per frame, and once folded
/// across the protobuf of many messages, which is the rolling checksum
/// `set_end` and `result_end` carry.
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

/// Consistent Overhead Byte Stuffing: the encoded bytes contain no zero, so a
/// zero can end a frame and a reader that lost bytes finds the next one.
pub fn cobs_encode(input: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(input.len() + input.len() / 254 + 2);
    let mut code_at = 0;
    out.push(0);
    let mut code: u8 = 1;
    for &byte in input {
        if byte == 0 {
            out[code_at] = code;
            code_at = out.len();
            out.push(0);
            code = 1;
            continue;
        }
        out.push(byte);
        code += 1;
        if code == 0xFF {
            out[code_at] = code;
            code_at = out.len();
            out.push(0);
            code = 1;
        }
    }
    out[code_at] = code;
    out
}

/// The inverse, or `None` for bytes that are not COBS.
pub fn cobs_decode(input: &[u8]) -> Option<Vec<u8>> {
    let mut out = Vec::with_capacity(input.len());
    let mut read = 0;
    while read < input.len() {
        let code = input[read];
        read += 1;
        if code == 0 {
            return None;
        }
        for _ in 1..code {
            let byte = *input.get(read)?;
            if byte == 0 {
                return None;
            }
            out.push(byte);
            read += 1;
        }
        // A zero was elided after every group but a full one and the last.
        if code != 0xFF && read < input.len() {
            out.push(0);
        }
    }
    Some(out)
}

/// A protobuf message, sealed into a frame: CRC, COBS, delimiter.
pub fn seal_frame(payload: &[u8]) -> Vec<u8> {
    let crc = crc16_ccitt(payload, CRC_INIT);
    let mut sealed = payload.to_vec();
    sealed.extend_from_slice(&crc.to_be_bytes());
    let mut frame = cobs_encode(&sealed);
    frame.push(0);
    frame
}

/// A frame came back that is not a message: not COBS, a bad CRC, too long, or
/// protobuf that does not decode. `code` is the board's own word for the same
/// refusal, which is what the serial monitor shows beside it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FramingError {
    pub code: &'static str,
    pub detail: String,
}

impl FramingError {
    pub fn new(code: &'static str, detail: impl Into<String>) -> Self {
        Self {
            code,
            detail: detail.into(),
        }
    }
}

impl std::fmt::Display for FramingError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.detail)
    }
}

impl std::error::Error for FramingError {}

/// The bytes between two delimiters, checked: the protobuf they carry.
pub fn open_frame(stuffed: &[u8]) -> Result<Vec<u8>, FramingError> {
    let Some(mut sealed) = cobs_decode(stuffed) else {
        return Err(FramingError::new(
            "bad_cobs",
            format!("{} bytes that are not COBS", stuffed.len()),
        ));
    };
    if sealed.len() < 2 {
        return Err(FramingError::new(
            "bad_cobs",
            "a frame too short to carry a CRC",
        ));
    }
    let crc_at = sealed.len() - 2;
    let claimed = u16::from_be_bytes([sealed[crc_at], sealed[crc_at + 1]]);
    sealed.truncate(crc_at);
    let actual = crc16_ccitt(&sealed, CRC_INIT);
    if claimed != actual {
        return Err(FramingError::new(
            "bad_crc",
            format!("frame says {claimed:04X}, bytes say {actual:04X}"),
        ));
    }
    Ok(sealed)
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
