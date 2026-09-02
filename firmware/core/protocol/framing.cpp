#include "protocol/framing.h"

#include "protocol/crc16.h"

namespace fsmd {
namespace {

// `,"crc":"XXXX"}` -- fixed width, which is the whole reason the protocol pins
// crc as the last member: the receiver finds it by arithmetic from the end of
// the line rather than by parsing its way to it.
constexpr size_t kCrcTailLen = 14;
constexpr char kCrcPrefix[] = ",\"crc\":\"";
constexpr size_t kCrcPrefixLen = 8;

}  // namespace

const char* frame_error_str(FrameError e) {
  switch (e) {
    case FrameError::None:
      return "ok";
    case FrameError::TooLong:
      return "line exceeded the line budget";
    case FrameError::NonAscii:
      return "line contained a non-ASCII byte";
    case FrameError::NotObject:
      return "line is not a JSON object";
    case FrameError::NoCrc:
      return "line has no trailing crc member";
    case FrameError::BadCrc:
      return "crc does not match the line";
  }
  return "unknown";
}

FrameError verify_frame(const char* line, size_t len, Frame* out) {
  if (len < kCrcTailLen + 1 || line[0] != '{' || line[len - 1] != '}')
    return FrameError::NotObject;

  const char* tail = line + len - kCrcTailLen;
  for (size_t i = 0; i < kCrcPrefixLen; ++i)
    if (tail[i] != kCrcPrefix[i]) return FrameError::NoCrc;
  if (tail[12] != '"') return FrameError::NoCrc;

  uint16_t claimed = 0;
  if (!crc16_from_hex(tail + kCrcPrefixLen, &claimed)) return FrameError::NoCrc;

  const size_t covered_len = len - kCrcTailLen;
  if (crc16_ccitt(line, covered_len) != claimed) return FrameError::BadCrc;

  if (out != nullptr) {
    out->covered = line;
    out->covered_len = covered_len;
  }
  return FrameError::None;
}

size_t finish_frame(char* buf, size_t len, size_t cap, bool newline) {
  const size_t needed = len + kCrcTailLen + (newline ? 1u : 0u);
  if (needed > cap) return 0;

  const uint16_t crc = crc16_ccitt(buf, len);
  for (size_t i = 0; i < kCrcPrefixLen; ++i) buf[len + i] = kCrcPrefix[i];
  crc16_to_hex(crc, buf + len + kCrcPrefixLen);
  buf[len + 12] = '"';
  buf[len + 13] = '}';
  if (newline) buf[len + 14] = '\n';
  return needed;
}

void LineReader::reset() {
  len_ = 0;
  status_ = FrameError::None;
  discarding_ = false;
  complete_ = false;
}

bool LineReader::feed(char c) {
  // A completed line stays readable until the next byte arrives, so the caller
  // can parse it without having to copy it out first.
  if (complete_) {
    len_ = 0;
    status_ = FrameError::None;
    complete_ = false;
  }

  if (c == '\n') {
    if (discarding_) {
      ++dropped_;
      discarding_ = false;
      complete_ = true;
      len_ = 0;
      return true;  // status_ already says why
    }
    // A \r immediately before the newline is accepted and ignored, so a
    // terminal program on the other end does not break the link.
    if (len_ > 0 && buf_[len_ - 1] == '\r') --len_;
    // A wholly empty line is not a message and not an error. \r\n handling and
    // idle keepalives both produce them, and answering each with an error would
    // make a cosmetic problem look like a fault.
    if (len_ == 0) return false;
    complete_ = true;
    return true;
  }

  if (discarding_) return false;

  if (static_cast<unsigned char>(c) >= 0x80) {
    status_ = FrameError::NonAscii;
    discarding_ = true;
    return false;
  }
  if (len_ >= cap_) {
    status_ = FrameError::TooLong;
    discarding_ = true;
    return false;
  }
  buf_[len_++] = c;
  return false;
}

}  // namespace fsmd
