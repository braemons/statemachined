// SPDX-License-Identifier: GPL-3.0-or-later
// Consistent Overhead Byte Stuffing, which is what makes a frame findable.
//
// A frame is COBS-encoded so that it contains no zero byte, and a zero ends it.
// A receiver that lost bytes -- a USB re-enumeration, a board that reset
// mid-frame -- therefore finds the start of the next frame without parsing
// anything: it waits for a zero. That is the job the newline did on the NDJSON
// wire, for a payload that is now binary and may contain any byte.
//
// The same encoding mousewheeld's board uses, so the two links have one shape.
#pragma once
#include <cstddef>
#include <cstdint>

namespace statemachined {

/// The most cobs_encode() can produce from `len` bytes, not counting the
/// delimiter: one code byte per 254 data bytes, and one to start.
constexpr size_t cobs_max_encoded(size_t len) { return len + len / 254 + 1; }

/// Encode `len` bytes of `in` into `out`, which holds at least
/// cobs_max_encoded(len). Returns the encoded length. Writes no delimiter.
size_t cobs_encode(const uint8_t* in, size_t len, uint8_t* out);

/// Decode `len` bytes, delimiter excluded, into `out`, which may be `in`:
/// decoding never writes ahead of where it reads. Returns the decoded length,
/// or 0 if the input is not valid COBS -- a zero inside it, or a code byte that
/// runs past its end.
size_t cobs_decode(const uint8_t* in, size_t len, uint8_t* out);

}  // namespace statemachined
