// SPDX-License-Identifier: AGPL-3.0-or-later
//! One command out, one reply back, and everything else handed to a sink.
//!
//! Strict request/response with **a single command in flight**, which is what
//! the protocol's one-deep duplicate memory assumes. Unsolicited messages —
//! `event`, `log`, `result_*` — can arrive between a command and its reply, so
//! the read loop matches on `in_reply_to` rather than on arrival order.

use std::time::{Duration, Instant};

use serde_json::Value;

use super::message_framing::{
    command_line, parse_reply, DeviceRefusedTheCommand, FramingError,
};
use super::message_vocabulary::{field, MsgType};
use super::serial_link::SerialLink;

pub const PROTOCOL_VERSION: i64 = 1;

pub const DEFAULT_REQUEST_TIMEOUT: Duration = Duration::from_secs(5);

/// Why a request did not produce a reply.
#[derive(Debug)]
pub enum RequestProblem {
    /// No reply carrying our `in_reply_to` arrived before the deadline.
    NoReplyInTime(String),
    /// The device refused the command, which is a normal outcome and not a bug.
    Refused(DeviceRefusedTheCommand),
    /// The link itself failed.
    Link(std::io::Error),
}

impl std::fmt::Display for RequestProblem {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NoReplyInTime(sentence) => f.write_str(sentence),
            Self::Refused(refusal) => write!(f, "{refusal}"),
            Self::Link(problem) => write!(f, "{problem}"),
        }
    }
}

impl std::error::Error for RequestProblem {}

impl From<std::io::Error> for RequestProblem {
    fn from(problem: std::io::Error) -> Self {
        Self::Link(problem)
    }
}

/// A session seed as `hex64`: 16 hex digits, **never a JSON number**.
///
/// 64 bits do not survive a double, and a seed that silently changes is a
/// reproducibility bug nobody would find.
pub fn random_seed() -> String {
    // Two words out of the OS, rather than a PRNG this would then have to seed
    // from somewhere. A session seed is drawn once per connection.
    let mut bytes = [0u8; 8];
    getrandom(&mut bytes);
    format!("{:016X}", u64::from_be_bytes(bytes))
}

fn getrandom(bytes: &mut [u8]) {
    use std::io::Read;
    std::fs::File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(bytes))
        .expect("/dev/urandom, which every rig this runs on has");
}

/// What to do with a message that is not the reply being waited for.
pub trait Sink {
    /// A message the device sent unprompted, **with the raw line**.
    ///
    /// The line is needed because a result's rolling checksum is over bytes, so
    /// a reader that only saw parsed messages could not check it — and the
    /// result chunks are exactly the messages that arrive unsolicited.
    fn unsolicited(&mut self, message: &Value, line: &str);

    /// A line that is not a message, or is one nobody asked for, and why.
    fn junk(&mut self, line: &str, why: &str);
}

/// A sink that drops everything, for a caller with nothing to do with it.
pub struct Discard;

impl Sink for Discard {
    fn unsolicited(&mut self, _message: &Value, _line: &str) {}
    fn junk(&mut self, _line: &str, _why: &str) {}
}

pub struct RequestResponseSession<S: Sink> {
    pub link: SerialLink,
    pub sink: S,
    /// Counting from **1, not 0**.
    ///
    /// A `message_id` of 0 is perfectly legal — the counter is a u16 that wraps
    /// through it — and current firmware answers it like any other. Firmware
    /// built before the `send_orphan_error` split read 0 as "no id could be
    /// read" and refused such a command without naming it, and boards in a rack
    /// are flashed when somebody gets to them. So this stays: it costs nothing
    /// and keeps a session's first command out of that hole. The unattributed
    /// reply is still handled below, for the wrap and for a device with its own
    /// version of the same bug.
    message_id: u16,
    pub hello_ack: Option<Value>,
}

impl<S: Sink> RequestResponseSession<S> {
    pub fn new(link: SerialLink, sink: S) -> Self {
        Self {
            link,
            sink,
            message_id: 1,
            hello_ack: None,
        }
    }

    pub(crate) fn next_message_id(&mut self) -> u16 {
        let message_id = self.message_id;
        self.message_id = self.message_id.wrapping_add(1);
        message_id
    }

    /// Is this the reply to the command with that `message_id`?
    ///
    /// `in_reply_to` is the answer when it is there. An `error` without one is
    /// taken as the reply anyway: the protocol requires `in_reply_to` on every
    /// reply, but **a refusal that arrives while exactly one command is
    /// outstanding is about that command whatever it is labelled**, and
    /// swallowing it would turn a clear "no hello yet" into a five-second
    /// silence.
    fn answers(message: &Value, message_id: u16) -> bool {
        match message.get(field::IN_REPLY_TO).and_then(Value::as_u64) {
            Some(in_reply_to) => in_reply_to == message_id as u64,
            None => msg_type_of(message) == Some(MsgType::Error),
        }
    }

    /// Send one command and return its reply, failing on `error`.
    ///
    /// **No retry.** The protocol makes a blind resend safe, but a retry
    /// nobody asked for turns "the board did not answer" into "the board
    /// answered eventually", and that is exactly the fact being measured.
    pub fn request(
        &mut self,
        msg_type: MsgType,
        timeout: Duration,
        fields: &[(&str, Value)],
    ) -> Result<Value, RequestProblem> {
        let message_id = self.next_message_id();
        let line = command_line(msg_type.as_str(), message_id as u64, fields);
        self.request_line(&line, message_id, timeout, msg_type.as_str())
    }

    /// The same, for a command already framed by the caller.
    ///
    /// A graph upload folds a rolling checksum over the exact bytes it sent, so
    /// it has to build the line itself and cannot let `request` build one it
    /// never sees. Everything below the framing — matching on `in_reply_to`,
    /// routing the unsolicited aside, failing on a refusal, not retrying — is
    /// the same and is not re-decided there.
    pub fn request_line(
        &mut self,
        line: &str,
        message_id: u16,
        timeout: Duration,
        what: &str,
    ) -> Result<Value, RequestProblem> {
        self.link.write_line(line)?;
        let deadline = Instant::now() + timeout;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(RequestProblem::NoReplyInTime(format!(
                    "no reply to {what} (message_id {message_id}) within {}s",
                    // Python's `%g`: a whole number of seconds reads as one.
                    trimmed(timeout.as_secs_f64())
                )));
            }
            let Some(raw) = self.link.read_line(Some(remaining))? else {
                continue;
            };
            let Some(message) = self.receive(&raw) else {
                continue;
            };
            if Self::answers(&message, message_id) {
                if msg_type_of(&message) == Some(MsgType::Error) {
                    if message.get(field::IN_REPLY_TO).is_none() {
                        self.sink.junk(
                            &raw,
                            "an error naming no message_id; taken as the reply anyway",
                        );
                    }
                    return Err(RequestProblem::Refused(
                        DeviceRefusedTheCommand::from_reply(&message),
                    ));
                }
                return Ok(message);
            }
            // A reply to somebody else's command, or one whose id we already
            // gave up on. Worth seeing rather than swallowing.
            let named = message
                .get(field::IN_REPLY_TO)
                .map(|value| value.to_string())
                .unwrap_or_else(|| "none".into());
            self.sink
                .junk(&raw, &format!("reply to message_id {named}, expected {message_id}"));
        }
    }

    /// Parse one received line, routing what is not a reply.
    pub fn receive(&mut self, line: &str) -> Option<Value> {
        if line.trim().is_empty() {
            return None;
        }
        let message = match parse_reply(line) {
            Ok(message) => message,
            Err(FramingError(why)) => {
                self.sink.junk(line, &why);
                return None;
            }
        };
        let kind = msg_type_of(&message);
        if kind.is_some_and(|kind| kind.is_unsolicited()) {
            self.sink.unsolicited(&message, line);
            return None;
        }
        // A `started` with nothing to reply to is a trial the *line* began. It
        // is the device reporting an event, not an answer.
        if kind.is_some_and(|kind| kind.is_unsolicited_when_unprompted())
            && message.get(field::IN_REPLY_TO).is_none()
        {
            self.sink.unsolicited(&message, line);
            return None;
        }
        Some(message)
    }

    /// Open a session — and take the rig from a board that was running on its
    /// own.
    ///
    /// **Opening the port does not do that; the greeting does.** It is the
    /// whole point of the handover that a serial monitor cannot trigger it.
    pub fn hello(
        &mut self,
        seed: Option<&str>,
        timeout: Duration,
    ) -> Result<Value, RequestProblem> {
        let seed = seed.map(str::to_string).unwrap_or_else(random_seed);
        let ack = self.request(
            MsgType::Hello,
            timeout,
            &[
                ("proto", Value::from(PROTOCOL_VERSION)),
                ("seed", Value::from(seed)),
            ],
        )?;
        self.hello_ack = Some(ack.clone());
        Ok(ack)
    }

    pub fn state(&mut self, timeout: Duration) -> Result<Value, RequestProblem> {
        self.request(MsgType::State, timeout, &[])
    }

    pub fn ping(&mut self, timeout: Duration) -> Result<Value, RequestProblem> {
        self.request(MsgType::Ping, timeout, &[])
    }
}

/// A duration as a person would write it: `5`, not `5.000000`.
fn trimmed(seconds: f64) -> String {
    let text = format!("{seconds}");
    text.trim_end_matches('0').trim_end_matches('.').to_string()
}

fn msg_type_of(message: &Value) -> Option<MsgType> {
    MsgType::named(message.get(field::MSG_TYPE)?.as_str()?)
}
