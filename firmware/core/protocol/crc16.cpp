// SPDX-License-Identifier: GPL-3.0-or-later
#include "protocol/crc16.h"

namespace statemachined {
namespace {

// Nibble table: 32 bytes of flash against 8 shifts per byte. The bitwise form
// costs ~2400 iterations on a 300-byte message, which is nothing next to a
// 100 us scan -- but serial work happens between scans, and the smaller
// constant is free here.
constexpr uint16_t kNibble[16] = {0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5,
                                  0x60C6, 0x70E7, 0x8108, 0x9129, 0xA14A, 0xB16B,
                                  0xC18C, 0xD1AD, 0xE1CE, 0xF1EF};

}  // namespace

uint16_t crc16_ccitt(const void* data, size_t len, uint16_t seed) {
  const uint8_t* p = static_cast<const uint8_t*>(data);
  uint16_t crc = seed;
  for (size_t i = 0; i < len; ++i) {
    crc = static_cast<uint16_t>((crc << 4) ^ kNibble[((crc >> 12) ^ (p[i] >> 4)) & 0x0F]);
    crc = static_cast<uint16_t>((crc << 4) ^ kNibble[((crc >> 12) ^ (p[i] & 0x0F)) & 0x0F]);
  }
  return crc;
}

}  // namespace statemachined
