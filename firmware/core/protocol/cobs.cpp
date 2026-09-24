// SPDX-License-Identifier: GPL-3.0-or-later
#include "protocol/cobs.h"

namespace statemachined {

size_t cobs_encode(const uint8_t* in, size_t len, uint8_t* out) {
  size_t code_at = 0;
  size_t write = 1;
  uint8_t code = 1;
  for (size_t read = 0; read < len; ++read) {
    if (in[read] == 0) {
      out[code_at] = code;
      code_at = write++;
      code = 1;
      continue;
    }
    out[write++] = in[read];
    if (++code == 0xFF) {
      // A full group of 254 non-zero bytes, which ends without an implied zero.
      out[code_at] = code;
      code_at = write++;
      code = 1;
    }
  }
  out[code_at] = code;
  return write;
}

size_t cobs_decode(const uint8_t* in, size_t len, uint8_t* out) {
  size_t read = 0;
  size_t write = 0;
  while (read < len) {
    const uint8_t code = in[read++];
    if (code == 0) return 0;
    for (uint8_t i = 1; i < code; ++i) {
      if (read >= len || in[read] == 0) return 0;
      out[write++] = in[read++];
    }
    // A zero was elided after every group but a full one and the last.
    if (code != 0xFF && read < len) out[write++] = 0;
  }
  return write;
}

}  // namespace statemachined
