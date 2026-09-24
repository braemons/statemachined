// SPDX-License-Identifier: GPL-3.0-or-later
// The frame layer. These tests are the promise docs/reference/protocol.md
// section 1 makes: a corrupt frame is refused whole, never acted on in part, a
// reader that lost bytes finds the next frame, and a link that is dropping
// frames says so instead of producing trials that quietly did not happen.
#include <cstdint>
#include <string>
#include <vector>

#include "doctest.h"
#include "protocol/crc16.h"
#include "protocol/framing.h"

using namespace statemachined;

namespace {

using Bytes = std::vector<uint8_t>;

Bytes frame_of(const Bytes& payload) {
  Bytes out(kMaxFrame);
  const size_t n = encode_frame(payload.data(), payload.size(), out.data());
  REQUIRE(n > 0);
  out.resize(n);
  return out;
}

/// What the reader made of a stream: each frame it reported, with its status.
struct Emitted {
  Bytes payload;
  FrameError status;
};

std::vector<Emitted> read_all(FrameReader& reader, const Bytes& stream) {
  std::vector<Emitted> out;
  for (uint8_t byte : stream) {
    if (!reader.feed(byte)) continue;
    out.push_back({Bytes(reader.payload(), reader.payload() + reader.len()), reader.status()});
  }
  return out;
}

std::vector<Emitted> read_all(const Bytes& stream) {
  FrameReader reader;
  return read_all(reader, stream);
}

Bytes concat(std::initializer_list<Bytes> parts) {
  Bytes out;
  for (const Bytes& part : parts) out.insert(out.end(), part.begin(), part.end());
  return out;
}

/// A payload with zeros in it, which is every protobuf message with a field
/// set to zero -- the case a newline-delimited wire never had to think about.
Bytes payload_of(size_t length, uint8_t salt = 0) {
  Bytes out(length);
  for (size_t i = 0; i < length; ++i) out[i] = static_cast<uint8_t>(i % 5 == 0 ? 0 : i + salt);
  return out;
}

}  // namespace

TEST_CASE("CRC-16/CCITT-FALSE matches its published check values") {
  // The vectors every implementation of this variant is checked against, so
  // that a future micro-optimisation of the nibble table is caught by
  // arithmetic rather than by a rig behaving strangely.
  CHECK(crc16_ccitt("123456789", 9) == 0x29B1);
  CHECK(crc16_ccitt("", 0) == 0xFFFF);
  CHECK(crc16_ccitt("A", 1) == 0xB915);
}

TEST_CASE("the seed makes the CRC an accumulator") {
  // set_end's checksum covers every message since set_begin without buffering
  // any of them, which only works if a split is invisible.
  const char* s = "123456789";
  const uint16_t whole = crc16_ccitt(s, 9);
  uint16_t acc = crc16_ccitt(s, 4);
  acc = crc16_ccitt(s + 4, 5, acc);
  CHECK(acc == whole);
}

TEST_CASE("a frame is the payload and its CRC, big-endian, stuffed and ended with a zero") {
  // The protocol document's own example, byte for byte, so the prose and the
  // code cannot disagree about which end of the CRC goes first.
  const Bytes payload{0x08, 0x29, 0xBA, 0x01, 0x00};  // message_id 41, ping {}
  const uint16_t crc = crc16_ccitt(payload.data(), payload.size());
  Bytes sealed = payload;
  sealed.push_back(static_cast<uint8_t>(crc >> 8));
  sealed.push_back(static_cast<uint8_t>(crc & 0xFF));
  Bytes expected(cobs_max_encoded(sealed.size()));
  expected.resize(cobs_encode(sealed.data(), sealed.size(), expected.data()));
  expected.push_back(0);
  CHECK(frame_of(payload) == expected);
  CHECK(frame_of(payload).back() == 0);
}

TEST_CASE("every payload length round-trips, up to the largest a frame holds") {
  for (size_t length = 0; length <= kMaxPayload; ++length) {
    const Bytes payload = payload_of(length, static_cast<uint8_t>(length));
    const Bytes frame = frame_of(payload);
    CHECK(frame.size() <= kMaxFrame);
    // The zero is the delimiter and appears nowhere else.
    for (size_t i = 0; i + 1 < frame.size(); ++i) REQUIRE(frame[i] != 0);
    const auto got = read_all(frame);
    REQUIRE(got.size() == 1);
    CHECK(got[0].status == FrameError::None);
    CHECK(got[0].payload == payload);
  }
}

TEST_CASE("a payload with no zero in it round-trips too, whatever its length") {
  // A run of more than 254 non-zero bytes is where COBS adds a code byte of
  // its own, and where encoding in place is closest to overwriting what it has
  // not read yet. A protobuf message of that length with no zero byte is
  // ordinary -- a pin map, a long result chunk.
  for (size_t length = 250; length <= kMaxPayload; ++length) {
    Bytes payload(length);
    for (size_t i = 0; i < length; ++i) payload[i] = static_cast<uint8_t>(1 + i % 255);
    const auto got = read_all(frame_of(payload));
    REQUIRE(got.size() == 1);
    CHECK(got[0].status == FrameError::None);
    CHECK(got[0].payload == payload);
  }
}

TEST_CASE(
    "encode_frame refuses a payload longer than a frame holds rather than truncating it") {
  const Bytes too_long(kMaxPayload + 1, 0x55);
  uint8_t out[kMaxFrame];
  CHECK(encode_frame(too_long.data(), too_long.size(), out) == 0);
}

TEST_CASE("the reader splits a stream into frames") {
  const Bytes a = payload_of(3), b = payload_of(40, 7), c = payload_of(0);
  const auto got = read_all(concat({frame_of(a), frame_of(b), frame_of(c)}));
  REQUIRE(got.size() == 3);
  CHECK(got[0].payload == a);
  CHECK(got[1].payload == b);
  CHECK(got[2].payload == c);
}

TEST_CASE("back-to-back delimiters are not a frame and not an error") {
  // How a sender resynchronises a receiver that may be mid-frame.
  FrameReader reader;
  const auto got =
      read_all(reader, concat({Bytes{0, 0, 0}, frame_of(payload_of(9)), Bytes{0}}));
  REQUIRE(got.size() == 1);
  CHECK(got[0].status == FrameError::None);
  CHECK(reader.dropped() == 0);
}

TEST_CASE("a corrupted frame is refused rather than partly believed") {
  const Bytes payload = payload_of(30, 3);
  const Bytes good = frame_of(payload);
  // Every single-byte corruption short of the delimiter, and every value it
  // could be corrupted to that is not the delimiter itself.
  for (size_t at = 0; at + 1 < good.size(); ++at) {
    for (int flip : {0x01, 0x80, 0xFF}) {
      Bytes bad = good;
      bad[at] = static_cast<uint8_t>(bad[at] ^ flip);
      if (bad[at] == 0) continue;
      const auto got = read_all(bad);
      REQUIRE(got.size() == 1);
      CHECK(got[0].status != FrameError::None);
      CHECK(got[0].payload.empty());
    }
  }
}

TEST_CASE("a CRC that does not match is named as one") {
  Bytes frame = frame_of(Bytes{0x11, 0x22, 0x33});
  // The CRC's low byte, which sits just before the delimiter and is not a
  // COBS code byte in a frame this short.
  frame[frame.size() - 2] = static_cast<uint8_t>(frame[frame.size() - 2] ^ 0x01);
  const auto got = read_all(frame);
  REQUIRE(got.size() == 1);
  CHECK(got[0].status == FrameError::BadCrc);
}

TEST_CASE("a frame too short to carry a CRC is not COBS the reader can use") {
  const auto got = read_all(Bytes{0x02, 0x11, 0x00});
  REQUIRE(got.size() == 1);
  CHECK(got[0].status == FrameError::BadCobs);
}

TEST_CASE("an overlong frame is refused, and the reader finds the next one") {
  Bytes stream(kMaxFrame + 40, 0x41);
  stream.push_back(0);
  const Bytes next = payload_of(12);
  stream = concat({stream, frame_of(next)});
  const auto got = read_all(stream);
  REQUIRE(got.size() == 2);
  CHECK(got[0].status == FrameError::TooLong);
  CHECK(got[1].status == FrameError::None);
  CHECK(got[1].payload == next);
}

TEST_CASE("bytes lost mid-frame cost that frame and no other") {
  // A USB re-enumeration, or a board reset halfway through a reply: the
  // receiver sees the tail of one frame and then the next whole one.
  const Bytes first = frame_of(payload_of(60, 1));
  const Bytes second_payload = payload_of(20, 2);
  const Bytes tail(first.begin() + 25, first.end());
  const auto got = read_all(concat({tail, frame_of(second_payload)}));
  REQUIRE(got.size() == 2);
  CHECK(got[0].status != FrameError::None);
  CHECK(got[1].payload == second_payload);
}

TEST_CASE("refused frames are counted so a bad link is visible") {
  FrameReader reader;
  Bytes bad = frame_of(payload_of(10));
  bad[1] = static_cast<uint8_t>(bad[1] ^ 0x40);
  read_all(reader, concat({bad, bad, frame_of(payload_of(4))}));
  CHECK(reader.dropped() == 2);
}

TEST_CASE("reset abandons a partial frame") {
  FrameReader reader;
  const Bytes frame = frame_of(payload_of(30));
  for (size_t i = 0; i < 10; ++i) CHECK_FALSE(reader.feed(frame[i]));
  reader.reset();
  const Bytes next = payload_of(8, 9);
  const auto got = read_all(reader, frame_of(next));
  REQUIRE(got.size() == 1);
  CHECK(got[0].status == FrameError::None);
  CHECK(got[0].payload == next);
}

TEST_CASE("every frame error has a message and a code") {
  for (FrameError e : {FrameError::TooLong, FrameError::BadCobs, FrameError::BadCrc}) {
    CHECK(std::string(frame_error_str(e)) != "unknown");
    CHECK(std::string(frame_error_code(e)) != "internal");
  }
  CHECK(std::string(frame_error_code(FrameError::TooLong)) == "too_long");
  CHECK(std::string(frame_error_code(FrameError::BadCobs)) == "bad_cobs");
  CHECK(std::string(frame_error_code(FrameError::BadCrc)) == "bad_crc");
}
