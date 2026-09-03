// SPDX-License-Identifier: GPL-3.0-or-later
#include "protocol/crc16.h"

namespace fsmd {
namespace {

// Nibble table: 32 bytes of flash against 8 shifts per byte. The bitwise form
// costs ~2400 iterations on a 300-byte message, which is nothing next to a
// 100 us scan -- but serial work happens between scans, and the smaller
// constant is free here.
constexpr uint16_t kNibble[16] = {0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5,
                                  0x60C6, 0x70E7, 0x8108, 0x9129, 0xA14A, 0xB16B,
                                  0xC18C, 0xD1AD, 0xE1CE, 0xF1EF};

constexpr char kHex[] = "0123456789ABCDEF";

int hex_value(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

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

void crc16_to_hex(uint16_t crc, char out[4]) {
  out[0] = kHex[(crc >> 12) & 0x0F];
  out[1] = kHex[(crc >> 8) & 0x0F];
  out[2] = kHex[(crc >> 4) & 0x0F];
  out[3] = kHex[crc & 0x0F];
}

bool crc16_from_hex(const char in[4], uint16_t* out) {
  uint16_t v = 0;
  for (int i = 0; i < 4; ++i) {
    const int d = hex_value(in[i]);
    if (d < 0) return false;
    v = static_cast<uint16_t>((v << 4) | static_cast<uint16_t>(d));
  }
  *out = v;
  return true;
}

}  // namespace fsmd
