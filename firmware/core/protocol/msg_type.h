// SPDX-License-Identifier: GPL-3.0-or-later
// The vocabulary of dev/PROTOCOL.md, as a type rather than as string literals
// scattered through the session.
//
// Two things this buys that a `const char*` does not. Dispatch becomes a switch
// the compiler checks, so a message type added to this enum and forgotten in
// the session is a warning rather than a command silently answered with
// `unknown_type`. And a reply cannot be built with a misspelled name: there is
// exactly one place each wire string is written, below, and both directions
// read it from there.
//
// The strings live in flash, not in the 32 KB, and nothing here allocates.
#pragma once
#include <cstdint>

namespace statemachined {

// Forward-declared rather than including json.h, which would be a cycle: the
// encoder writes these key names, so json.cpp includes this header. The
// vocabulary sits below the encoder, not beside it.
struct JsonSpan;

/// Every `msg_type` on the wire, in both directions.
///
/// Host commands and device messages share one enum rather than splitting into
/// two. They are one namespace on the wire -- `msg_type` is a single field with
/// a single set of values -- and a device that answered a command with a name
/// only the host is supposed to send would be a bug this way round too.
enum class MsgType : uint8_t {
  // Host -> device.
  Hello,
  SetBegin,
  SetEnd,
  GraphBegin,
  GraphDist,
  GraphState,
  GraphTransition,
  GraphAction,
  GraphEnd,
  Configure,
  Start,
  Cancel,
  Ping,
  State,
  Wiring,

  // Device -> host.
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

  /// A `msg_type` this firmware does not know. Never written to the wire: it is
  /// what a name that matched nothing parses to, and it is answered with
  /// `unknown_type`.
  Unknown,
};

/// The wire string for a type. The single definition of each name.
const char* msg_type_name(MsgType t);

/// The type a received `msg_type` names, or MsgType::Unknown.
MsgType msg_type_from(JsonSpan name);

/// The field names the framing itself owns, spelled once.
///
/// `msg_type` says which message this is, `message_id` identifies this line so
/// that a resend of it can be recognised, and `in_reply_to` carries the
/// `message_id` of the command a reply answers. See dev/PROTOCOL.md 1.2 for
/// why `message_id` is emphatically not a sequence number: it orders nothing,
/// a gap in it is not an error, and its whole job is making a blind retry
/// safe.
constexpr const char* kMsgTypeKey = "msg_type";
constexpr const char* kMessageIdKey = "message_id";
constexpr const char* kInReplyToKey = "in_reply_to";

}  // namespace statemachined
