// SPDX-License-Identifier: GPL-3.0-or-later
// The protocol's line layer: bytes in, verified lines out, and the CRC put back
// on the way out. Everything here is below JSON -- it knows a line has a `crc`
// member last and nothing else about what a message means.
//
// See dev/PROTOCOL.md, section 1.
#pragma once
#include <cstddef>
#include <cstdint>

#include "config.h"

namespace fsmd {

enum class FrameError : uint8_t {
  None = 0,
  TooLong,    ///< exceeded kMaxLine; discarded through the next newline
  NonAscii,   ///< a byte >= 0x80. Non-ASCII text belongs in log, \uXXXX-escaped
  NotObject,  ///< does not open with '{' and close with '}'
  NoCrc,      ///< no trailing ,"crc":"XXXX"
  BadCrc,     ///< the field is well-formed and does not match the payload
};

const char* frame_error_str(FrameError e);

/// The CRC-covered prefix of a verified line: everything before `,"crc":`.
/// Kept because graph_end's and result_end's checksums accumulate over exactly
/// these bytes, message after message, without buffering any of them.
struct Frame {
  const char* covered = nullptr;
  size_t covered_len = 0;
};

/// Check one assembled line, newline already stripped. The whole line -- `crc`
/// member included -- is what gets handed to the JSON parser afterwards; an
/// unknown member is ignored by rule, so the parser needs no special case for
/// it.
FrameError verify_frame(const char* line, size_t len, Frame* out);

/// Close an outgoing message. `buf[0..len)` holds the object *without* its
/// closing brace -- `{"t":"pong","seq":16` -- and this appends
/// `,"crc":"XXXX"}` and, if `newline`, the terminator. Returns 0 if the result
/// would not fit in `cap`, which is a caller bug rather than a wire condition:
/// every message the firmware emits is sized to fit kMaxLine by construction.
size_t finish_frame(char* buf, size_t len, size_t cap, bool newline = true);

/// Assembles incoming bytes into lines.
///
/// Byte at a time on purpose: on the device this runs from whatever the USB CDC
/// hands over, in chunks nobody chooses, and a reader that needs a whole line in
/// one call would need a second buffer to make that true.
class LineReader {
 public:
  LineReader(char* buf, size_t cap) : buf_(buf), cap_(cap) {}

  /// Returns true when a line is complete. `status()` then says whether it
  /// arrived intact; on anything but None the line is not parsable and `len()`
  /// is 0. A line that overflowed or carried a non-ASCII byte is still reported
  /// -- silently swallowing it would leave the host waiting for an answer that
  /// never comes.
  bool feed(char c);

  const char* line() const { return buf_; }
  size_t len() const { return len_; }
  FrameError status() const { return status_; }

  /// Bytes dropped since construction, for state_report. A link that is
  /// dropping lines should be visible to whoever is debugging the rig rather
  /// than inferred from trials that did not happen.
  uint32_t dropped() const { return dropped_; }

  void reset();

 private:
  char* buf_;
  size_t cap_;
  size_t len_ = 0;
  uint32_t dropped_ = 0;
  FrameError status_ = FrameError::None;
  bool discarding_ = false;
  bool complete_ = false;
};

}  // namespace fsmd
