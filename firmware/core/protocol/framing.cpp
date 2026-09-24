// SPDX-License-Identifier: GPL-3.0-or-later
#include "protocol/framing.h"

#include "protocol/crc16.h"

namespace statemachined {

const char* frame_error_str(FrameError e) {
  switch (e) {
    case FrameError::None:
      return "ok";
    case FrameError::TooLong:
      return "frame exceeded the frame budget";
    case FrameError::BadCobs:
      return "frame is not valid COBS";
    case FrameError::BadCrc:
      return "crc does not match the frame";
  }
  return "unknown";
}

const char* frame_error_code(FrameError e) {
  switch (e) {
    case FrameError::None:
      return "internal";
    case FrameError::TooLong:
      return "too_long";
    case FrameError::BadCobs:
      return "bad_cobs";
    case FrameError::BadCrc:
      return "bad_crc";
  }
  return "internal";
}

size_t encode_frame(const uint8_t* payload, size_t len, uint8_t* out) {
  if (len > kMaxPayload) return 0;
  // Sealed in place at the end of `out` and stuffed forwards from its start.
  // COBS writes at most one byte ahead of where it reads for every 254 it
  // reads, so starting the source that far along leaves the reads always ahead
  // of the writes -- and saves the 500-byte copy a second buffer would cost.
  const size_t sealed_len = len + 2;
  uint8_t* sealed = out + (kMaxFrame - 1 - sealed_len);
  for (size_t i = len; i-- > 0;) sealed[i] = payload[i];
  const uint16_t crc = crc16_ccitt(sealed, len);
  sealed[len] = static_cast<uint8_t>(crc >> 8);
  sealed[len + 1] = static_cast<uint8_t>(crc & 0xFF);
  const size_t encoded = cobs_encode(sealed, sealed_len, out);
  out[encoded] = 0;
  return encoded + 1;
}

bool FrameReader::feed(uint8_t byte) {
  if (byte != 0) {
    if (fill_ < sizeof(buffer_)) {
      buffer_[fill_++] = byte;
    } else {
      overflowed_ = true;
    }
    return false;
  }

  // A delimiter: whatever was collected is one frame.
  const size_t collected = fill_;
  const bool overflowed = overflowed_;
  fill_ = 0;
  overflowed_ = false;
  len_ = 0;

  // Back-to-back delimiters are how a sender resynchronises a receiver, so an
  // empty frame is not an error and is not reported.
  if (collected == 0 && !overflowed) return false;

  if (overflowed) {
    status_ = FrameError::TooLong;
    ++dropped_;
    return true;
  }
  const size_t decoded = cobs_decode(buffer_, collected, buffer_);
  if (decoded < 2) {
    status_ = FrameError::BadCobs;
    ++dropped_;
    return true;
  }
  const size_t payload_len = decoded - 2;
  const uint16_t sent =
      static_cast<uint16_t>((buffer_[payload_len] << 8) | buffer_[payload_len + 1]);
  if (crc16_ccitt(buffer_, payload_len) != sent) {
    status_ = FrameError::BadCrc;
    ++dropped_;
    return true;
  }
  len_ = payload_len;
  status_ = FrameError::None;
  return true;
}

void FrameReader::reset() {
  fill_ = 0;
  len_ = 0;
  overflowed_ = false;
  status_ = FrameError::None;
}

}  // namespace statemachined
