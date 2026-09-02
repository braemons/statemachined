// The line layer. These tests are the promise dev/PROTOCOL.md section 1 makes:
// a corrupt line is refused whole, never acted on in part, and a link that is
// dropping lines says so instead of producing trials that quietly did not
// happen.
#include <cstring>
#include <string>
#include <vector>

#include "doctest.h"
#include "protocol/crc16.h"
#include "protocol/framing.h"

using namespace fsmd;

namespace {

/// A message built the way the firmware builds one: body without the closing
/// brace, then finish_frame puts the crc and the brace on.
std::string framed(const std::string& body_without_brace, bool newline = true) {
  char buf[kMaxLine];
  REQUIRE(body_without_brace.size() < sizeof(buf));
  memcpy(buf, body_without_brace.data(), body_without_brace.size());
  const size_t n = finish_frame(buf, body_without_brace.size(), sizeof(buf), newline);
  REQUIRE(n > 0);
  return std::string(buf, n);
}

/// Feed a whole string and return the lines the reader emitted, each with the
/// status it carried.
struct Emitted {
  std::string text;
  FrameError status;
};
std::vector<Emitted> read_all(const std::string& in, size_t cap = kMaxLine) {
  std::vector<char> storage(cap);
  LineReader r(storage.data(), cap);
  std::vector<Emitted> out;
  for (char c : in)
    if (r.feed(c)) out.push_back({std::string(r.line(), r.len()), r.status()});
  return out;
}

}  // namespace

TEST_CASE("CRC-16/CCITT-FALSE matches its published check values") {
  // The two vectors every implementation of this variant is checked against.
  // Named here so that a future micro-optimisation of the nibble table is
  // caught by arithmetic rather than by a rig behaving strangely.
  CHECK(crc16_ccitt("123456789", 9) == 0x29B1);
  CHECK(crc16_ccitt("", 0) == 0xFFFF);
  CHECK(crc16_ccitt("A", 1) == 0xB915);
}

TEST_CASE("the seed makes the CRC an accumulator") {
  // graph_end's checksum covers every message since graph_begin without
  // buffering any of them, which only works if a split is invisible.
  const char* s = "123456789";
  const uint16_t whole = crc16_ccitt(s, 9);
  uint16_t acc = crc16_ccitt(s, 4);
  acc = crc16_ccitt(s + 4, 5, acc);
  CHECK(acc == whole);
}

TEST_CASE("hex round-trips and refuses what is not hex") {
  char h[4];
  crc16_to_hex(0x29B1, h);
  CHECK(std::string(h, 4) == "29B1");
  crc16_to_hex(0x000F, h);
  CHECK(std::string(h, 4) == "000F");

  uint16_t v = 0;
  CHECK(crc16_from_hex("29B1", &v));
  CHECK(v == 0x29B1);
  CHECK(crc16_from_hex("29b1", &v));  // lowercase accepted on the way in
  CHECK(v == 0x29B1);
  // A malformed crc field is a bad line, not a zero CRC.
  CHECK_FALSE(crc16_from_hex("29G1", &v));
  CHECK_FALSE(crc16_from_hex("    ", &v));
}

TEST_CASE("finish_frame writes the tail the protocol specifies") {
  const std::string line = framed(R"({"t":"ping","seq":41)", false);
  CHECK(line == R"({"t":"ping","seq":41,"crc":"C083"})");

  // And the CRC covers the payload and not the tail, which is the whole point
  // of pinning crc as the last member.
  CHECK(crc16_ccitt(R"({"t":"ping","seq":41)", 20) == 0xC083);
}

TEST_CASE("a well-formed line verifies and names the covered bytes") {
  const std::string line = framed(R"({"t":"ping","seq":41)", false);
  Frame f;
  REQUIRE(verify_frame(line.data(), line.size(), &f) == FrameError::None);
  CHECK(std::string(f.covered, f.covered_len) == R"({"t":"ping","seq":41)");
  CHECK(f.covered_len == line.size() - 14);
}

TEST_CASE("a corrupted line is refused rather than partly believed") {
  std::string line = framed(R"({"t":"cancel","seq":9,"trial_id":193)", false);
  REQUIRE(verify_frame(line.data(), line.size(), nullptr) == FrameError::None);

  SUBCASE("a flipped payload byte") {
    line[15] = '8';  // somewhere inside the seq
    CHECK(verify_frame(line.data(), line.size(), nullptr) == FrameError::BadCrc);
  }
  SUBCASE("a flipped crc digit") {
    line[line.size() - 3] = (line[line.size() - 3] == '0') ? '1' : '0';
    CHECK(verify_frame(line.data(), line.size(), nullptr) == FrameError::BadCrc);
  }
  SUBCASE("a crc that is not hex") {
    line[line.size() - 3] = 'Z';
    CHECK(verify_frame(line.data(), line.size(), nullptr) == FrameError::NoCrc);
  }
  SUBCASE("no crc member at all") {
    const std::string bare = R"({"t":"ping","seq":41})";
    CHECK(verify_frame(bare.data(), bare.size(), nullptr) == FrameError::NoCrc);
  }
  SUBCASE("not an object") {
    const std::string arr = R"(["t","ping","crc":"C083"])";
    CHECK(verify_frame(arr.data(), arr.size(), nullptr) == FrameError::NotObject);
    CHECK(verify_frame("", 0, nullptr) == FrameError::NotObject);
    CHECK(verify_frame("{}", 2, nullptr) == FrameError::NotObject);
  }
}

TEST_CASE("a crc member that is not last is not found") {
  // The protocol requires crc last so the receiver can locate it by arithmetic
  // from the end of the line. A sender that puts it elsewhere is refused rather
  // than accommodated -- accommodating it would mean parsing before verifying,
  // which is the ordering this layer exists to prevent.
  const std::string s = R"({"crc":"C083","t":"ping","seq":41})";
  CHECK(verify_frame(s.data(), s.size(), nullptr) == FrameError::NoCrc);
}

TEST_CASE("the reader splits a stream into lines") {
  const std::string stream = framed(R"({"t":"ping","seq":1)") +
                             framed(R"({"t":"ping","seq":2)") +
                             framed(R"({"t":"ping","seq":3)");
  const auto lines = read_all(stream);
  REQUIRE(lines.size() == 3);
  for (const auto& l : lines) {
    CHECK(l.status == FrameError::None);
    CHECK(verify_frame(l.text.data(), l.text.size(), nullptr) == FrameError::None);
  }
  CHECK(lines[1].text.find(R"("seq":2)") != std::string::npos);
}

TEST_CASE("a trailing carriage return is ignored") {
  std::string line = framed(R"({"t":"ping","seq":41)", false);
  const auto lines = read_all(line + "\r\n");
  REQUIRE(lines.size() == 1);
  CHECK(lines[0].status == FrameError::None);
  CHECK(verify_frame(lines[0].text.data(), lines[0].text.size(), nullptr) == FrameError::None);
}

TEST_CASE("an empty line is not a message and not an error") {
  const auto lines = read_all("\n\r\n\n" + framed(R"({"t":"ping","seq":1)") + "\n\n");
  REQUIRE(lines.size() == 1);
  CHECK(lines[0].status == FrameError::None);
}

TEST_CASE("an overlong line is discarded through the next newline") {
  // And crucially the message *after* it still arrives: a burst of noise must
  // cost the link one message, not the session.
  std::string noise(200, 'x');
  const std::string good = framed(R"({"t":"ping","seq":7)");
  const auto lines = read_all(noise + "\n" + good, 64);
  REQUIRE(lines.size() == 2);
  CHECK(lines[0].status == FrameError::TooLong);
  CHECK(lines[0].text.empty());  // nothing partial is handed up
  CHECK(lines[1].status == FrameError::None);
  CHECK(lines[1].text.find(R"("seq":7)") != std::string::npos);
}

TEST_CASE("a non-ASCII byte fails the line, not the session") {
  std::string bad = R"({"t":"log","m":")";
  bad += static_cast<char>(0xC3);
  bad += static_cast<char>(0xA9);
  bad += "\"}\n";
  const std::string good = framed(R"({"t":"ping","seq":7)");
  const auto lines = read_all(bad + good);
  REQUIRE(lines.size() == 2);
  CHECK(lines[0].status == FrameError::NonAscii);
  CHECK(lines[1].status == FrameError::None);
}

TEST_CASE("dropped lines are counted so a bad link is visible") {
  char buf[64];
  LineReader r(buf, sizeof(buf));
  CHECK(r.dropped() == 0);
  const std::string junk(200, 'x');
  for (char c : junk) r.feed(c);
  CHECK(r.feed('\n'));
  CHECK(r.dropped() == 1);
  CHECK(r.status() == FrameError::TooLong);
}

TEST_CASE("a completed line survives until the next byte") {
  char buf[64];
  LineReader r(buf, sizeof(buf));
  const std::string line = framed(R"({"t":"ping","seq":1)");
  size_t i = 0;
  for (; i < line.size(); ++i)
    if (r.feed(line[i])) break;
  REQUIRE(i + 1 == line.size());
  const std::string first(r.line(), r.len());
  CHECK(verify_frame(first.data(), first.size(), nullptr) == FrameError::None);

  // Feeding the next message must not append to it.
  const std::string second = framed(R"({"t":"ping","seq":2)");
  bool done = false;
  for (char c : second) done = r.feed(c);
  REQUIRE(done);
  CHECK(std::string(r.line(), r.len()).find(R"("seq":2)") != std::string::npos);
  CHECK(std::string(r.line(), r.len()).find(R"("seq":1)") == std::string::npos);
}

TEST_CASE("reset abandons a partial line") {
  // hello resets the device to idle, and a reconnecting bridge may well have
  // left half a line in the buffer. Resuming into the middle of it would splice
  // two sessions' bytes into one message.
  char buf[64];
  LineReader r(buf, sizeof(buf));
  for (char c : std::string(R"({"t":"pin)")) CHECK_FALSE(r.feed(c));
  r.reset();
  CHECK(r.len() == 0);
  CHECK(r.status() == FrameError::None);

  bool done = false;
  for (char c : framed(R"({"t":"ping","seq":1)")) done = r.feed(c);
  REQUIRE(done);
  CHECK(verify_frame(r.line(), r.len(), nullptr) == FrameError::None);
}

TEST_CASE("finish_frame refuses to overflow rather than truncating") {
  char buf[24];
  const std::string body = R"({"t":"ping","seq":1)";
  memcpy(buf, body.data(), body.size());
  CHECK(finish_frame(buf, body.size(), sizeof(buf), true) == 0);
}

TEST_CASE("every frame error has a message") {
  const FrameError every[] = {FrameError::None,      FrameError::TooLong, FrameError::NonAscii,
                              FrameError::NotObject, FrameError::NoCrc,   FrameError::BadCrc};
  for (FrameError e : every) {
    const char* m = frame_error_str(e);
    REQUIRE(m != nullptr);
    CHECK(m[0] != '\0');
  }
}
