// SPDX-License-Identifier: AGPL-3.0-or-later
//! The board: the wire it speaks, and how this daemon speaks it.
//!
//! **The riskiest part of the port** (`RUST_REWRITE_PLAN.md` §4.1), and the part
//! with the most help available: mousewheeld's `link/` and `device/` solve the
//! same problem — a board on a serial line, framed messages, a request/response
//! session — in the same family, in Rust.
//!
//! The framing is checked against `daemon/tests/unit/wire_vectors.json`, which
//! is golden against the firmware's own CRC rather than against any host
//! implementation.

pub mod message_framing;
pub mod request_response_session;
pub mod serial_link;
pub mod message_vocabulary;
