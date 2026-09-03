// SPDX-License-Identifier: GPL-3.0-or-later
#include "protocol/msg_type.h"

#include "protocol/json.h"

namespace statemachined {
namespace {

/// Indexed by MsgType, in the enum's own order, and the static_assert below
/// holds that shape: a type added to the enum without a name here fails to
/// compile rather than going out on the wire unnamed. Unknown is last in the
/// enum and has no entry, which is what makes the count check work.
const char* const kNames[] = {
    "hello",
    "set_begin",
    "set_end",
    "graph_begin",
    "graph_dist",
    "graph_state",
    "graph_transition",
    "graph_action",
    "graph_end",
    "configure",
    "start",
    "cancel",
    "ping",
    "state",
    "wiring",

    "hello_ack",
    "ack",
    "set_ok",
    "armed",
    "started",
    "cancel_ack",
    "result_begin",
    "result_path",
    "result_end",
    "event",
    "error",
    "log",
    "pong",
    "state_report",
    "visit",
};

constexpr uint8_t kKnown = static_cast<uint8_t>(MsgType::Unknown);
static_assert(sizeof(kNames) / sizeof(kNames[0]) == kKnown,
              "every MsgType but Unknown needs exactly one wire name");

}  // namespace

const char* msg_type_name(MsgType t) {
  const uint8_t i = static_cast<uint8_t>(t);
  return i < kKnown ? kNames[i] : "unknown";
}

MsgType msg_type_from(JsonSpan name) {
  // A linear scan over 26 short strings, on a message that has just been
  // CRC-checked and parsed. Ordering the table by how often a type arrives
  // would save a handful of comparisons on a path that is not a trial's, and
  // cost the table the property that makes it checkable: that its order is the
  // enum's.
  for (uint8_t i = 0; i < kKnown; ++i)
    if (json_str_eq(name, kNames[i])) return static_cast<MsgType>(i);
  return MsgType::Unknown;
}

}  // namespace statemachined
