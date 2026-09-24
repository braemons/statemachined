// SPDX-License-Identifier: AGPL-3.0-or-later
//! One refusal shape, for every rpc.
//!
//! The port of `servicers/refusals.py`. A status code is a category and a
//! message is a sentence; `error` is the part a client switches on and
//! `context` names what to change. So `statemachined.v1.Error` travels as
//! itself, in the trailing metadata entry `statemachined-error-bin`, and the
//! message keeps `detail (context)` for grpcurl and logs.
//!
//! **The trailer is not decoration.** `client/python` tells "the daemon
//! answered and has no board" from "nothing answered at all" by whether it is
//! there: both are `unavailable`, and only one of them is worth waiting out.
//! A daemon that sent the code without the trailer would have every client
//! believe the daemon itself was down.
//!
//! **The codes are Python's**, including where Python's choice is arguable —
//! a board that does not answer in time is `internal` there, because nothing
//! maps `NoReplyInTime`. Improving that is a change to make in both daemons,
//! after the port, in its own diff (`dev/RUST_PORT.md` §1).

use prost::Message;
use tonic::metadata::{MetadataMap, MetadataValue};

use crate::device::message_framing::DeviceRefusedTheCommand;
use crate::device::request_response_session::RequestProblem;
use crate::device::statemachined_device::DeviceProblem;
use crate::graph_set_compiler::CompileError;
use crate::model::graph_definition::Refused;
use crate::store::StoreProblem;
use crate::wire::statemachined::v1 as wire;

/// Where the typed refusal rides. `-bin` is gRPC's own spelling for a metadata
/// value that is bytes rather than ASCII.
pub const REFUSAL_METADATA_KEY: &str = "statemachined-error-bin";

/// Why a call was refused, in this daemon's own words — not an HTTP status.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Category {
    /// `unavailable`. **The one a caller may retry unchanged.**
    NoBoardAttached,
    /// `failed_precondition`: the daemon is not in a state where this means
    /// anything.
    WrongMoment,
    /// `invalid_argument`: understood and refused.
    BadRequest,
    /// `not_found`.
    NoSuchThing,
    /// `internal`: this daemon broke, not the caller.
    TheDaemonBroke,
}

impl Category {
    fn code(self) -> tonic::Code {
        match self {
            Self::NoBoardAttached => tonic::Code::Unavailable,
            Self::WrongMoment => tonic::Code::FailedPrecondition,
            Self::BadRequest => tonic::Code::InvalidArgument,
            Self::NoSuchThing => tonic::Code::NotFound,
            Self::TheDaemonBroke => tonic::Code::Internal,
        }
    }
}

/// A call this daemon will not make, with everything a caller needs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    pub category: Category,
    pub error: String,
    pub detail: String,
    /// Never empty: where there is nothing more specific it repeats `error`,
    /// because "which field" with no answer is worse than a coarse one.
    pub context: String,
}

impl Refusal {
    pub fn new(
        category: Category,
        error: impl Into<String>,
        detail: impl Into<String>,
        context: impl Into<String>,
    ) -> Self {
        let error = error.into();
        let context = context.into();
        Self {
            category,
            context: if context.is_empty() {
                error.clone()
            } else {
                context
            },
            error,
            detail: detail.into(),
        }
    }

    pub fn no_board_attached(detail: impl Into<String>) -> Self {
        Self::new(Category::NoBoardAttached, "not_connected", detail, "device")
    }

    fn the_daemon_broke(detail: impl Into<String>) -> Self {
        Self::new(Category::TheDaemonBroke, "internal", detail, "")
    }
}

impl From<Refusal> for tonic::Status {
    fn from(refusal: Refusal) -> Self {
        let message = format!("{} ({})", refusal.detail, refusal.context);
        let body = wire::Error {
            error: refusal.error,
            detail: refusal.detail,
            context: refusal.context,
        };
        let mut metadata = MetadataMap::new();
        metadata.insert_bin(
            REFUSAL_METADATA_KEY,
            MetadataValue::from_bytes(&body.encode_to_vec()),
        );
        tonic::Status::with_metadata(refusal.category.code(), message, metadata)
    }
}

/// A refusal's sentence, without the `(context)` the status message adds.
///
/// Read back out of the trailer, where the refusal travels as itself; a
/// status that carries none is its message.
pub fn detail_of(status: &tonic::Status) -> String {
    status
        .metadata()
        .get_bin(REFUSAL_METADATA_KEY)
        .and_then(|value| value.to_bytes().ok())
        .and_then(|bytes| wire::Error::decode(bytes.as_ref()).ok())
        .map(|body| body.detail)
        .unwrap_or_else(|| status.message().to_string())
}

/// The board's own refusal, in the board's own words.
///
/// `error` is the device's code rather than this daemon's paraphrase: those
/// are the words its documentation uses, and a caller switching on them should
/// be switching on the device's answer.
impl From<DeviceRefusedTheCommand> for Refusal {
    fn from(refused: DeviceRefusedTheCommand) -> Self {
        let detail = if refused.message.is_empty() {
            refused.to_string()
        } else {
            refused.message.clone()
        };
        Refusal::new(Category::WrongMoment, refused.code, detail, refused.context)
    }
}

impl From<RequestProblem> for Refusal {
    fn from(problem: RequestProblem) -> Self {
        match problem {
            RequestProblem::Refused(refused) => refused.into(),
            // Python has no rule for either, so they reach the caller as
            // `internal`. See the module docstring.
            other => Refusal::the_daemon_broke(other.to_string()),
        }
    }
}

impl From<DeviceProblem> for Refusal {
    fn from(problem: DeviceProblem) -> Self {
        match problem {
            DeviceProblem::NotConnected(sentence) => Refusal::no_board_attached(sentence),
            DeviceProblem::LineMap(sentence) => Refusal::new(
                Category::BadRequest,
                "line_map_does_not_match_the_board",
                sentence,
                "line_map",
            ),
            DeviceProblem::Request(problem) => problem.into(),
            // A `ValueError` and an `OSError` in Python, neither in its table.
            DeviceProblem::WrongBoard(sentence) => Refusal::the_daemon_broke(sentence),
            DeviceProblem::Link(problem) => Refusal::the_daemon_broke(problem.to_string()),
        }
    }
}

impl From<CompileError> for Refusal {
    fn from(problem: CompileError) -> Self {
        match problem {
            // "This set has no graph called X" and "this set does not fit on
            // the board" are different things to fix.
            CompileError::NotInSet(sentence) => {
                Refusal::new(Category::WrongMoment, "graph_not_in_set", sentence, "graph")
            }
            CompileError::Set(sentence) => {
                Refusal::new(Category::BadRequest, "does_not_fit", sentence, "graphs")
            }
        }
    }
}

/// Which store a problem came from, for the words the refusal uses.
#[derive(Debug, Clone, Copy)]
pub enum StoreKind {
    Graph,
    StateMachineConfig,
}

/// A store's failure, in the words `refusals.py` and the store servicers use.
pub fn store_refusal(kind: StoreKind, problem: StoreProblem) -> Refusal {
    let (missing, missing_context, mismatch, unreadable) = match kind {
        StoreKind::Graph => (
            "no_such_graph",
            "graph_name",
            "graph_name_mismatch",
            "bad_graph",
        ),
        StoreKind::StateMachineConfig => (
            "no_such_state_machine_config",
            "config_name",
            "config_name_mismatch",
            "bad_state_machine_config",
        ),
    };
    match problem {
        StoreProblem::NotStored(sentence) => {
            Refusal::new(Category::NoSuchThing, missing, sentence, missing_context)
        }
        StoreProblem::BadName(sentence) => {
            Refusal::new(Category::BadRequest, mismatch, sentence, "name")
        }
        StoreProblem::Unreadable(Refused(sentence)) => {
            Refusal::new(Category::BadRequest, unreadable, sentence, "text")
        }
        StoreProblem::Io(sentence) => Refusal::the_daemon_broke(sentence),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_refusal_travels_as_itself_in_the_trailer() {
        let status: tonic::Status =
            Refusal::new(Category::BadRequest, "does_not_fit", "too big", "graphs").into();
        assert_eq!(status.code(), tonic::Code::InvalidArgument);
        assert_eq!(status.message(), "too big (graphs)");
        let bytes = status
            .metadata()
            .get_bin(REFUSAL_METADATA_KEY)
            .expect("the trailer")
            .to_bytes()
            .unwrap();
        let body = wire::Error::decode(bytes.as_ref()).unwrap();
        assert_eq!(
            (
                body.error.as_str(),
                body.detail.as_str(),
                body.context.as_str()
            ),
            ("does_not_fit", "too big", "graphs")
        );
    }

    #[test]
    fn a_refusal_with_no_context_names_its_error_instead() {
        assert_eq!(
            Refusal::new(Category::WrongMoment, "busy", "x", "").context,
            "busy"
        );
    }

    #[test]
    fn the_board_saying_no_is_not_the_board_being_absent() {
        let refused = DeviceRefusedTheCommand {
            code: "busy".into(),
            message: "a trial is armed or running".into(),
            context: "graph upload".into(),
        };
        let refusal = Refusal::from(DeviceProblem::Request(RequestProblem::Refused(refused)));
        assert_eq!(refusal.category, Category::WrongMoment);
        assert_eq!(refusal.error, "busy");
        assert_eq!(refusal.context, "graph upload");
    }
}
