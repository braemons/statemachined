// SPDX-License-Identifier: GPL-3.0-or-later
// COBS: what makes a frame findable after bytes were lost. The vectors are the
// ones every implementation is checked against, from Cheshire and Baker's paper
// and the Wikipedia table that reproduces it.
#include <cstdint>
#include <vector>

#include "doctest.h"
#include "protocol/cobs.h"

using namespace statemachined;

namespace {

using Bytes = std::vector<uint8_t>;

Bytes encode(const Bytes& in) {
  Bytes out(cobs_max_encoded(in.size()));
  out.resize(cobs_encode(in.data(), in.size(), out.data()));
  return out;
}

Bytes decode(const Bytes& in) {
  Bytes out(in.size());
  out.resize(cobs_decode(in.data(), in.size(), out.data()));
  return out;
}

}  // namespace

TEST_CASE("COBS encodes the published vectors") {
  CHECK(encode({0x00}) == Bytes{0x01, 0x01});
  CHECK(encode({0x00, 0x00}) == Bytes{0x01, 0x01, 0x01});
  CHECK(encode({0x00, 0x11, 0x00}) == Bytes{0x01, 0x02, 0x11, 0x01});
  CHECK(encode({0x11, 0x22, 0x00, 0x33}) == Bytes{0x03, 0x11, 0x22, 0x02, 0x33});
  CHECK(encode({0x11, 0x22, 0x33, 0x44}) == Bytes{0x05, 0x11, 0x22, 0x33, 0x44});
  CHECK(encode({0x11, 0x00, 0x00, 0x00}) == Bytes{0x02, 0x11, 0x01, 0x01, 0x01});
  CHECK(encode({}) == Bytes{0x01});
}

TEST_CASE("a run of 254 non-zero bytes is a full group, and the next byte opens another") {
  Bytes in(254);
  for (size_t i = 0; i < in.size(); ++i) in[i] = static_cast<uint8_t>(i + 1);
  const Bytes out = encode(in);
  REQUIRE(out.size() == 256);
  CHECK(out[0] == 0xFF);
  CHECK(out[255] == 0x01);
  CHECK(decode(out) == in);

  in.push_back(0x42);
  const Bytes longer = encode(in);
  CHECK(longer.size() == 257);
  CHECK(longer[255] == 0x02);
  CHECK(longer[256] == 0x42);
  CHECK(decode(longer) == in);
}

TEST_CASE("no encoded frame contains a zero, which is what makes the delimiter unique") {
  for (size_t length = 0; length < 600; length += 7) {
    Bytes in(length);
    for (size_t i = 0; i < length; ++i) in[i] = static_cast<uint8_t>((i * 37) % 3 == 0 ? 0 : i);
    const Bytes out = encode(in);
    CHECK(out.size() <= cobs_max_encoded(length));
    for (uint8_t byte : out) CHECK(byte != 0);
    CHECK(decode(out) == in);
  }
}

TEST_CASE("decoding in place, which is how the frame reader uses it") {
  const Bytes in{0x11, 0x00, 0x22, 0x00, 0x00, 0x33};
  Bytes buffer = encode(in);
  const size_t n = cobs_decode(buffer.data(), buffer.size(), buffer.data());
  buffer.resize(n);
  CHECK(buffer == in);
}

TEST_CASE("input that is not COBS is refused rather than decoded into something") {
  // A zero inside a frame: the delimiter can never be part of one.
  CHECK(decode({0x03, 0x11, 0x00}).empty());
  // A code byte that promises more bytes than there are.
  CHECK(decode({0x05, 0x11, 0x22}).empty());
  // A code of zero.
  CHECK(decode({0x00}).empty());
}
