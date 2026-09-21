// SPDX-License-Identifier: AGPL-3.0-or-later
//! The protocol's vocabulary, as constants rather than string literals.
//!
//! The mirror of `firmware/core/protocol/msg_type.h`. A misspelling is a
//! compile error here rather than a command the device answers with
//! `unknown_type`.
//!
//! Not shared with `emulation/tests/statemachined_protocol.py`, deliberately.
//! That module is a second implementation written from the protocol document so
//! that a test asks the device an independent question; handing it this
//! vocabulary would make both ends agree by construction, which is exactly the
//! agreement the emulator test exists to not assume.

/// The members the framing itself owns.
///
/// `MESSAGE_ID` is emphatically **not** a sequence number — it orders nothing,
/// a gap in it is not an error, and its whole job is letting a resend be
/// recognised as one.
pub mod field {
    pub const MSG_TYPE: &str = "msg_type";
    pub const MESSAGE_ID: &str = "message_id";
    pub const IN_REPLY_TO: &str = "in_reply_to";
    pub const CRC: &str = "crc";
}

/// Every `msg_type` on the wire, in both directions.
///
/// An enum rather than bare constants, so that a match over what arrived is
/// exhaustive and a message type added to the protocol shows up as a compile
/// error in every place that dispatches on one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MsgType {
    Hello,
    SetBegin,
    SetEnd,
    GraphBegin,
    GraphDist,
    GraphState,
    GraphTransition,
    GraphAction,
    GraphTimer,
    GraphEnd,
    Configure,
    Start,
    Cancel,
    Ping,
    State,
    Wiring,
    Timers,
    Pins,
    Autorun,
    Save,
    HelloAck,
    Ack,
    SetOk,
    Armed,
    Started,
    CancelAck,
    ResultBegin,
    ResultPath,
    ResultEnd,
    Event,
    Error,
    Log,
    Pong,
    StateReport,
    Visit,
    PinMap,
    AutorunOk,
    Saved,
}

impl MsgType {
    /// The word on the wire.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Hello => "hello",
            Self::SetBegin => "set_begin",
            Self::SetEnd => "set_end",
            Self::GraphBegin => "graph_begin",
            Self::GraphDist => "graph_dist",
            Self::GraphState => "graph_state",
            Self::GraphTransition => "graph_transition",
            Self::GraphAction => "graph_action",
            Self::GraphTimer => "graph_timer",
            Self::GraphEnd => "graph_end",
            Self::Configure => "configure",
            Self::Start => "start",
            Self::Cancel => "cancel",
            Self::Ping => "ping",
            Self::State => "state",
            Self::Wiring => "wiring",
            Self::Timers => "timers",
            Self::Pins => "pins",
            Self::Autorun => "autorun",
            Self::Save => "save",
            Self::HelloAck => "hello_ack",
            Self::Ack => "ack",
            Self::SetOk => "set_ok",
            Self::Armed => "armed",
            Self::Started => "started",
            Self::CancelAck => "cancel_ack",
            Self::ResultBegin => "result_begin",
            Self::ResultPath => "result_path",
            Self::ResultEnd => "result_end",
            Self::Event => "event",
            Self::Error => "error",
            Self::Log => "log",
            Self::Pong => "pong",
            Self::StateReport => "state_report",
            Self::Visit => "visit",
            Self::PinMap => "pin_map",
            Self::AutorunOk => "autorun_ok",
            Self::Saved => "saved",
        }
    }

    /// The type a line says it is, or `None` for one this build does not know.
    ///
    /// Not `FromStr`: that trait's error type would have to carry a case that
    /// is not an error here.
    ///
    /// `None` rather than an error: a device newer than the daemon serving it
    /// is a real situation, and an unknown message is one to log and skip
    /// rather than one to tear a link down over.
    pub fn named(word: &str) -> Option<Self> {
        Some(match word {
            "hello" => Self::Hello,
            "set_begin" => Self::SetBegin,
            "set_end" => Self::SetEnd,
            "graph_begin" => Self::GraphBegin,
            "graph_dist" => Self::GraphDist,
            "graph_state" => Self::GraphState,
            "graph_transition" => Self::GraphTransition,
            "graph_action" => Self::GraphAction,
            "graph_timer" => Self::GraphTimer,
            "graph_end" => Self::GraphEnd,
            "configure" => Self::Configure,
            "start" => Self::Start,
            "cancel" => Self::Cancel,
            "ping" => Self::Ping,
            "state" => Self::State,
            "wiring" => Self::Wiring,
            "timers" => Self::Timers,
            "pins" => Self::Pins,
            "autorun" => Self::Autorun,
            "save" => Self::Save,
            "hello_ack" => Self::HelloAck,
            "ack" => Self::Ack,
            "set_ok" => Self::SetOk,
            "armed" => Self::Armed,
            "started" => Self::Started,
            "cancel_ack" => Self::CancelAck,
            "result_begin" => Self::ResultBegin,
            "result_path" => Self::ResultPath,
            "result_end" => Self::ResultEnd,
            "event" => Self::Event,
            "error" => Self::Error,
            "log" => Self::Log,
            "pong" => Self::Pong,
            "state_report" => Self::StateReport,
            "visit" => Self::Visit,
            "pin_map" => Self::PinMap,
            "autorun_ok" => Self::AutorunOk,
            "saved" => Self::Saved,
            _ => return None,
        })
    }
}

impl std::fmt::Display for MsgType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

impl MsgType {
    /// Types that are never a reply: they arrive when the device has something
    /// to say, which may be between a command and its answer.
    pub fn is_unsolicited(&self) -> bool {
        matches!(
            self,
            Self::Event
                | Self::Log
                | Self::ResultBegin
                | Self::ResultPath
                | Self::ResultEnd
                | Self::Visit
        )
    }

    /// Types that are a *reply* when they carry `in_reply_to` and an event when
    /// they do not.
    ///
    /// `started` is the only one, and it is one because a trial can begin two
    /// ways: because the host said so, and because a start line went high on a
    /// board arming its own trials. Routing it by type alone would make
    /// `start_trial` unable to recognise its own reply.
    pub fn is_unsolicited_when_unprompted(&self) -> bool {
        matches!(self, Self::Started)
    }
}
