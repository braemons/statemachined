// SPDX-License-Identifier: GPL-3.0-or-later
// The frame layer: a protobuf message, sealed with a CRC, stuffed with COBS and
// ended with a zero. Both directions, identically:
//
//     COBS( protobuf message ‖ CRC-16, big-endian ) ‖ 0x00
//
// A frame is checked whole before a byte of it is decoded. One that is too
// long, is not COBS, or whose CRC does not match is refused and never acted on,
// not even partially: a truncated graph_state is exactly what this exists to
// catch. See docs/reference/protocol.md section 1.
#pragma once
#include <cstddef>
#include <cstdint>

#include "config.h"
#include "protocol/cobs.h"

namespace statemachined {

/// The longest protobuf message a frame of kMaxFrame bytes can carry: the
/// delimiter, the CRC and COBS's worst-case overhead come off the top.
constexpr size_t kMaxPayload = kMaxFrame - 1 - 2 - (kMaxFrame / 254 + 1);
static_assert(cobs_max_encoded(kMaxPayload + 2) + 1 <= kMaxFrame, "kMaxPayload fits a frame");

enum class FrameError : uint8_t {
  None = 0,
  TooLong,  ///< more than kMaxFrame bytes before a delimiter
  BadCobs,  ///< not valid COBS, or too short to hold a CRC
  BadCrc,   ///< the CRC does not match the bytes it covers
};

/// The person's half of a refusal, for `error`'s `message`.
const char* frame_error_str(FrameError e);
/// The machine's half, for `error`'s `code`: `too_long`, `bad_cobs`, `bad_crc`.
const char* frame_error_code(FrameError e);

/// Seal `len` bytes of protobuf into a frame in `out`, which holds kMaxFrame.
/// Returns the frame's length, delimiter included, or 0 if the payload is
/// longer than kMaxPayload -- which the static_asserts on the generated sizes
/// make a firmware bug rather than a wire condition.
size_t encode_frame(const uint8_t* payload, size_t len, uint8_t* out);

/// Bytes in, checked payloads out. Resynchronises on the next delimiter after
/// anything it refuses, which is all a reader that lost bytes needs to do.
class FrameReader {
 public:
  /// Returns true when a delimiter ended a frame. status() then says whether it
  /// arrived intact; on anything but None there is no payload. A refused frame
  /// is still reported -- silently swallowing it would leave the host waiting
  /// for an answer that never comes.
  bool feed(uint8_t byte);

  /// The protobuf bytes, CRC removed. Valid after feed() returned true with
  /// status() None, until the next feed().
  const uint8_t* payload() const { return buffer_; }
  size_t len() const { return len_; }
  FrameError status() const { return status_; }

  /// Frames refused since construction, for state_report. A link that is
  /// dropping frames should be visible to whoever is debugging the rig rather
  /// than inferred from trials that did not happen.
  uint32_t dropped() const { return dropped_; }

  /// Forget a frame in progress. What a new session does: bytes from before a
  /// reconnect belong to a host that is gone.
  void reset();

 private:
  uint8_t buffer_[kMaxFrame];
  size_t fill_ = 0;
  size_t len_ = 0;
  bool overflowed_ = false;
  uint32_t dropped_ = 0;
  FrameError status_ = FrameError::None;
};

}  // namespace statemachined
