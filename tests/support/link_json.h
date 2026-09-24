// SPDX-License-Identifier: GPL-3.0-or-later
// The link's messages, to and from the JSON the NDJSON wire carried.
//
// For the tests only. A test writes a command as the object it always was --
// `{"msg_type":"configure","message_id":4,"trial_id":193,...` -- and this makes
// it the HostMessage the board now decodes, with every member under the name
// it has in link.proto. A reply comes back rendered as the line the board would
// have written, member for member and in the same order, without the CRC.
//
// So a test reads as the protocol document does, and says what the session
// does rather than how nanopb spells it. What this cannot carry is a member of
// the wrong JSON type -- protobuf has no way to send a string where a number
// goes -- and the tests that sent one are gone with the wire that could.
#pragma once
#include <cstdint>
#include <string>
#include <vector>

#include "protocol/link_messages.h"

namespace statemachined::test {

using Bytes = std::vector<uint8_t>;

/// `text` as the HostMessage it describes. False, with `why`, for a member
/// this cannot carry. A `msg_type` link.proto has no body for is returned with
/// no body, which is what the board gets from a newer daemon's command.
bool host_message_from_json(const std::string& text, link::HostMessage* out, std::string* why);

/// The protobuf bytes of a host message: what a frame carries, and what the
/// upload's checksum folds.
Bytes encode_host_message(const link::HostMessage& message);

/// `payload` sealed into a frame, delimiter included.
Bytes frame_of(const Bytes& payload);

/// Read one frame the board sent. False if it is not a frame, or not a
/// DeviceMessage -- either of which is a board bug these tests exist to catch.
bool decode_device_frame(const Bytes& frame, link::DeviceMessage* out, Bytes* payload);

/// The line the NDJSON board would have written for `message`, newline
/// included and CRC left off.
std::string json_of(const link::DeviceMessage& message);

}  // namespace statemachined::test
