// SPDX-License-Identifier: GPL-3.0-or-later
// A frame's payload: a fixed header, then one message body.
//
//   offset  size  field
//   0       1     type         the body's field number in link.proto's
//                              HostMessage / DeviceMessage `oneof body`
//   1       1     flags        bit 0: `in_reply_to` follows; no others yet
//   2       2     message_id   little-endian; the sender's counter
//   4       2     in_reply_to  little-endian; only when flags bit 0 is set
//   ...           body         the message itself, protobuf, not enveloped
//
// The header is not protobuf because it did not need to be and it was the
// most expensive thing the board encoded: nanopb walks every member of a
// `oneof` to encode one, and a body inside an envelope is a nested message,
// which it encodes twice. A `pong` cost five times its own encode. See
// link.proto's note on the envelopes.
//
// The session decodes into and encodes from nanopb's envelope structs all the
// same -- a union of every body, which is exactly what a board wants to hold
// one message in -- and this is what moves between those structs and bytes.
// Shared by the session and the host tests' JSON bridge, so the two cannot
// disagree about a byte.
#pragma once

#include <cstddef>
#include <cstdint>

#include "protocol/link_messages.h"

namespace statemachined {
namespace link {

struct Header {
  /// The body's field number; `which_body` in nanopb's envelope structs.
  pb_size_t type = 0;
  uint16_t message_id = 0;
  bool has_in_reply_to = false;
  uint16_t in_reply_to = 0;
};

constexpr size_t kHeaderBytes = 4;
constexpr size_t kMaxHeaderBytes = kHeaderBytes + 2;
constexpr uint8_t kFlagInReplyTo = 0x01;

enum class PayloadError : uint8_t {
  None = 0,
  TooShort,     ///< shorter than its own header
  UnknownType,  ///< a type this firmware does not know; the header is valid
  BadBody,      ///< the body did not decode as its type
};

/// `m` must be zeroed first: the body is decoded without nanopb's default
/// initialisation, which walks every field of every body to set a zero. On
/// UnknownType and BadBody `h` is filled in, so the refusal can name the
/// command it refuses.
PayloadError decode_host_payload(const uint8_t* bytes, size_t n, Header* h, HostMessage* m,
                                 const char** why = nullptr);
PayloadError decode_device_payload(const uint8_t* bytes, size_t n, Header* h, DeviceMessage* m,
                                   const char** why = nullptr);

/// The body is `m.which_body`'s; `h.type` is ignored and taken from it.
/// Returns the payload's length, or 0 if it did not fit `cap` or did not
/// encode -- a firmware bug, which the static_asserts on the generated sizes
/// rule out for the session's own buffers.
size_t encode_device_payload(const Header& h, const DeviceMessage& m, uint8_t* out, size_t cap);
size_t encode_host_payload(const Header& h, const HostMessage& m, uint8_t* out, size_t cap);

}  // namespace link
}  // namespace statemachined
