// SPDX-License-Identifier: GPL-3.0-or-later
// The JSON layer. It is the front door for untrusted input -- everything the
// host sends arrives through here -- so these tests spend most of their effort
// on what it must refuse, not on what it must accept.
#include <deque>
#include <string>
#include <vector>

#include "doctest.h"
#include "protocol/framing.h"
#include "protocol/json.h"

using namespace statemachined;

namespace {

// The reader borrows the line rather than copying it -- every JsonSpan points
// into the caller's buffer -- so the text has to outlive the object built over
// it. On the device that is the line buffer, which lives until the next line
// arrives. Here it is a deque, whose elements never move.
std::deque<std::string> g_texts;
const JsonObject& parse(const std::string& s) {
  g_texts.push_back(s);
  static std::deque<JsonObject> objects;
  objects.emplace_back(g_texts.back().data(), g_texts.back().size());
  return objects.back();
}

}  // namespace

TEST_CASE("a protocol message parses into its members") {
  const auto o = parse(R"({"t":"configure","seq":41,"trial_id":193,"cap_ms":30000})");
  REQUIRE(o.valid());
  CHECK(o.size() == 4);

  JsonSpan t;
  REQUIRE(o.str("t", &t));
  CHECK(json_str_eq(t, "configure"));

  uint16_t seq = 0;
  uint32_t id = 0;
  int32_t cap = 0;
  CHECK(o.u16("seq", &seq));
  CHECK(seq == 41);
  CHECK(o.u32("trial_id", &id));
  CHECK(id == 193);
  CHECK(o.i32("cap_ms", &cap));
  CHECK(cap == 30000);
}

TEST_CASE("an unknown member is ignored, which is how the protocol grows") {
  const auto o = parse(R"({"t":"ping","seq":1,"something_from_2027":{"a":[1,2,3]}})");
  REQUIRE(o.valid());
  uint16_t seq = 0;
  CHECK(o.u16("seq", &seq));
  CHECK(seq == 1);
  CHECK(o.type_of("nope") == JsonType::Missing);
}

TEST_CASE("absent and wrong-typed are one case at the call site") {
  // Both mean "the message does not say what I need", and both answer
  // bad_json. Distinguishing them would give the caller a choice it has no use
  // for.
  const auto o = parse(R"({"a":"7","b":true,"c":null,"d":[1]})");
  REQUIRE(o.valid());
  uint32_t v = 0;
  CHECK_FALSE(o.u32("a", &v));  // a string that looks like a number
  CHECK_FALSE(o.u32("b", &v));
  CHECK_FALSE(o.u32("c", &v));
  CHECK_FALSE(o.u32("d", &v));
  CHECK_FALSE(o.u32("missing", &v));
}

TEST_CASE("null is a value, not an absence") {
  // A state's `terminal` is null when the state is not terminal, and that is a
  // different thing from a graph_state that forgot to say.
  const auto o = parse(R"({"terminal":null,"timeout":null})");
  REQUIRE(o.valid());
  CHECK(o.is_null("terminal"));
  CHECK_FALSE(o.is_null("absent"));
  CHECK(o.type_of("absent") == JsonType::Missing);
  CHECK(o.type_of("terminal") == JsonType::Null);
}

TEST_CASE("integers are checked against the width they are asked for") {
  const auto o = parse(R"({"big":70000,"neg":-5,"max32":4294967295,"i8":-128,"over8":300})");
  REQUIRE(o.valid());
  uint32_t u = 0;
  uint16_t h = 0;
  uint8_t b = 0;
  int8_t s = 0;
  int32_t i = 0;

  CHECK(o.u32("big", &u));
  CHECK(u == 70000);
  CHECK_FALSE(o.u16("big", &h));  // refused, not truncated to 4464
  CHECK(o.u32("max32", &u));
  CHECK(u == 4294967295u);
  CHECK(o.i32("neg", &i));
  CHECK(i == -5);
  CHECK_FALSE(o.u32("neg", &u));  // a negative is not an unsigned
  CHECK(o.i8("i8", &s));
  CHECK(s == -128);
  CHECK_FALSE(o.u8("over8", &b));
}

TEST_CASE("a float is refused rather than truncated") {
  // This protocol has no floats. A host sending 1.5 ms should be told so, not
  // quietly given 1 -- the second is a timing bug that survives review.
  for (const char* bad : {R"({"a":1.5})", R"({"a":1e3})", R"({"a":1E3})", R"({"a":-0.0})"}) {
    const auto o = parse(bad);
    CHECK_FALSE(o.valid());
    CHECK(o.error() == JsonError::Malformed);
  }
}

TEST_CASE("a 64-bit seed crosses as hex, because a JSON number would lose it") {
  const auto o = parse(R"({"seed":"0123456789ABCDEF","short":"ff","bad":"12G4","empty":""})");
  REQUIRE(o.valid());
  uint64_t v = 0;
  REQUIRE(o.hex64("seed", &v));
  CHECK(v == 0x0123456789ABCDEFull);
  CHECK(o.hex64("short", &v));
  CHECK(v == 0xFF);
  CHECK_FALSE(o.hex64("bad", &v));
  CHECK_FALSE(o.hex64("empty", &v));

  // The full 64-bit range survives, which is the entire point.
  const auto top = parse(R"({"seed":"FFFFFFFFFFFFFFFF"})");
  REQUIRE(top.valid());
  REQUIRE(top.hex64("seed", &v));
  CHECK(v == 0xFFFFFFFFFFFFFFFFull);
}

TEST_CASE("nested objects and arrays read back") {
  const auto o = parse(R"({"timeout":{"dist":3,"target":2},"debounce_ms":[0,2,2,0]})");
  REQUIRE(o.valid());

  JsonObject timeout;
  REQUIRE(o.object("timeout", &timeout));
  uint8_t dist = 0, target = 0;
  CHECK(timeout.u8("dist", &dist));
  CHECK(dist == 3);
  CHECK(timeout.u8("target", &target));
  CHECK(target == 2);

  JsonArray a;
  REQUIRE(o.array("debounce_ms", &a));
  std::vector<int32_t> got;
  int32_t v = 0;
  while (a.next_i32(&v)) got.push_back(v);
  CHECK(got == std::vector<int32_t>{0, 2, 2, 0});
}

TEST_CASE("an empty array yields nothing and is not an error") {
  const auto o = parse(R"({"opts":[],"patch":[]})");
  REQUIRE(o.valid());
  JsonArray a;
  REQUIRE(o.array("opts", &a));
  int32_t v = 0;
  CHECK_FALSE(a.next_i32(&v));
}

TEST_CASE("an array of rows reads as the result path does") {
  const auto o = parse(R"({"p":[[0,"timeout",255,500],[1,"transition",2,0]]})");
  REQUIRE(o.valid());
  JsonArray rows;
  REQUIRE(o.array("p", &rows));

  JsonSpan row;
  JsonType t = JsonType::Missing;
  REQUIRE(rows.next(&row, &t));
  REQUIRE(t == JsonType::Array);
  JsonArray first(row.p, row.n);
  int32_t v = 0;
  REQUIRE(first.next_i32(&v));
  CHECK(v == 0);
  JsonSpan cause;
  JsonType ct = JsonType::Missing;
  REQUIRE(first.next(&cause, &ct));
  CHECK(ct == JsonType::String);
  CHECK(json_str_eq(cause, "timeout"));

  REQUIRE(rows.next(&row, &t));
  CHECK_FALSE(rows.next(&row, &t));
}

TEST_CASE("malformed input is refused whole") {
  struct Case {
    const char* text;
    JsonError err;
  };
  const Case cases[] = {
      {R"(["not","an","object"])", JsonError::NotObject},
      {R"(not json at all)", JsonError::NotObject},
      {"", JsonError::NotObject},
      {R"({"a":1)", JsonError::Malformed},    // unterminated
      {R"({"a":})", JsonError::Malformed},    // no value
      {R"({"a" 1})", JsonError::Malformed},   // no colon
      {R"({a:1})", JsonError::Malformed},     // unquoted key
      {R"({"a":1,})", JsonError::Malformed},  // trailing comma
      {R"({"a":"unterminated})", JsonError::Malformed},
      {R"({"a":tru})", JsonError::Malformed},
      {R"({"a":[1,2)", JsonError::Malformed},
      {R"({"a":1}garbage)", JsonError::Malformed},  // trailing rubbish
      {R"({"a":01})", JsonError::Malformed},        // leading zero
      {R"({"a":-})", JsonError::Malformed},
  };
  for (const Case& c : cases) {
    const auto o = parse(c.text);
    CAPTURE(c.text);
    CHECK_FALSE(o.valid());
    CHECK(o.error() == c.err);
  }
}

TEST_CASE("a fault inside a nested value fails the whole document") {
  // A graph_state carries its timeout as a nested object and its debounce as an
  // array. Validating only the outer shape would let a malformed inner one
  // through, to be discovered by whatever read it next.
  const char* bad[] = {
      R"({"timeout":{"dist":1,}})", R"({"timeout":{"dist" 1}})", R"({"timeout":{dist:1}})",
      R"({"timeout":{"dist":1)",    R"({"opts":[1,2,]})",        R"({"opts":[1 2]})",
      R"({"p":[[1,2],[3,)",         R"({"opts":[1.5]})",
  };
  for (const char* text : bad) {
    CAPTURE(text);
    CHECK_FALSE(parse(text).valid());
  }
}

TEST_CASE("an array stops at the first element it cannot read") {
  // Not "skips": an element it cannot parse means the sender and this reader
  // disagree about the shape, and continuing would report a path that is not
  // the one that happened.
  const auto& o = parse(R"({"a":[1,2,3]})");
  REQUIRE(o.valid());
  JsonArray arr;
  REQUIRE(o.array("a", &arr));
  uint32_t u = 0;
  CHECK(arr.next_u32(&u));
  CHECK(u == 1);
  CHECK(arr.next_u32(&u));
  CHECK(arr.next_u32(&u));
  CHECK(u == 3);
  CHECK_FALSE(arr.next_u32(&u));

  // A negative is not an unsigned, and the array stops there rather than
  // wrapping it into four billion.
  const auto& neg = parse(R"({"a":[1,-2,3]})");
  JsonArray b;
  REQUIRE(neg.array("a", &b));
  CHECK(b.next_u32(&u));
  CHECK_FALSE(b.next_u32(&u));

  // rewind() starts again from the front.
  b.rewind();
  CHECK(b.next_u32(&u));
  CHECK(u == 1);

  // A string where a number belongs ends it too: the row shapes in this
  // protocol are fixed, so a type surprise is a disagreement, not a variant.
  const auto& mixed = parse(R"({"a":["timeout",1]})");
  JsonArray c;
  REQUIRE(mixed.array("a", &c));
  CHECK_FALSE(c.next_u32(&u));
  CHECK_FALSE(c.next_u32(&u));

  // A structurally broken element ends the iteration rather than being skipped.
  const std::string raw = R"([1,tru,3])";
  JsonArray broken(raw.data(), raw.size());
  int32_t v = 0;
  CHECK(broken.next_i32(&v));
  CHECK_FALSE(broken.next_i32(&v));
}

TEST_CASE("json_str_eq decodes every escape JSON defines") {
  const std::string text = R"(a\bb\ff\rr\tt\/s)";
  CHECK(json_str_eq({text.data(), text.size()}, "a\bb\ff\rr\tt/s"));

  // An escape JSON does not define is a mismatch, not a literal backslash.
  // scan_string refuses these on the way in; this is the second door.
  const std::string bad = R"(\q)";
  CHECK_FALSE(json_str_eq({bad.data(), bad.size()}, "\\q"));
  CHECK_FALSE(json_str_eq({bad.data(), bad.size()}, "q"));

  // A \u escape above ASCII cannot match a literal in this codebase, and
  // decoding it would need a buffer to hold the UTF-8 it becomes.
  const std::string high = R"(\u00E9)";
  CHECK_FALSE(json_str_eq({high.data(), high.size()}, "e"));
  const std::string nul = R"(\u0000)";
  CHECK_FALSE(json_str_eq({nul.data(), nul.size()}, ""));
  const std::string trunc = R"(\u00)";
  CHECK_FALSE(json_str_eq({trunc.data(), trunc.size()}, "A"));
  const std::string nothex = R"(\u00ZZ)";
  CHECK_FALSE(json_str_eq({nothex.data(), nothex.size()}, "A"));
  const std::string low = R"(A)";
  CHECK(json_str_eq({low.data(), low.size()}, "A"));
}

TEST_CASE("nesting deeper than the protocol allows is refused") {
  CHECK(parse(R"({"a":{"b":{"c":1}}})").valid());
  const auto deep = parse(R"({"a":{"b":{"c":{"d":{"e":1}}}}})");
  CHECK_FALSE(deep.valid());
  CHECK(deep.error() == JsonError::TooDeep);
}

TEST_CASE("a duplicate key is refused rather than resolved") {
  // Last-wins and first-wins are both defensible and neither is discoverable
  // from reading the message. A configure carrying two trial_ids is a bug in
  // the sender and is answered as one.
  const auto o = parse(R"({"trial_id":1,"trial_id":2})");
  CHECK_FALSE(o.valid());
  CHECK(o.error() == JsonError::DuplicateKey);
}

TEST_CASE("more members than the protocol defines is refused") {
  std::string s = "{";
  for (int i = 0; i < kJsonMaxMembers + 1; ++i) {
    if (i) s += ",";
    s += "\"k" + std::to_string(i) + "\":1";
  }
  s += "}";
  const auto o = parse(s);
  CHECK_FALSE(o.valid());
  CHECK(o.error() == JsonError::TooManyKeys);
}

TEST_CASE("a raw control byte inside a string is not a string") {
  std::string s = R"({"m":"a)";
  s += '\n';
  s += R"(b"})";
  CHECK_FALSE(parse(s).valid());
}

TEST_CASE("escapes are decoded on comparison, without a buffer") {
  const auto o = parse(R"({"a":"line\nbreak","b":"quo\"te","c":"sl\/ash","d":"ABC"})");
  REQUIRE(o.valid());
  JsonSpan s;
  REQUIRE(o.str("a", &s));
  CHECK(json_str_eq(s, "line\nbreak"));
  REQUIRE(o.str("b", &s));
  CHECK(json_str_eq(s, "quo\"te"));
  REQUIRE(o.str("c", &s));
  CHECK(json_str_eq(s, "sl/ash"));
  REQUIRE(o.str("d", &s));
  CHECK(json_str_eq(s, "ABC"));
  CHECK_FALSE(json_str_eq(s, "AB"));
  CHECK_FALSE(json_str_eq(s, "ABCD"));

  CHECK_FALSE(parse(R"({"a":"\q"})").valid());
  CHECK_FALSE(parse(R"({"a":"\u00ZZ"})").valid());
}

TEST_CASE("whitespace between tokens is accepted") {
  const auto o = parse("{ \"t\" : \"ping\" , \"seq\" : 3 }");
  REQUIRE(o.valid());
  uint16_t seq = 0;
  CHECK(o.u16("seq", &seq));
  CHECK(seq == 3);
}

TEST_CASE("an empty object is valid and says nothing") {
  const auto o = parse("{}");
  CHECK(o.valid());
  CHECK(o.size() == 0);
  CHECK(o.type_of("t") == JsonType::Missing);
}

TEST_CASE("the writer emits a framed message the reader accepts") {
  char buf[kMaxLine];
  JsonWriter w(buf, sizeof(buf));
  w.begin("armed", 12);
  w.req(41);
  w.key_u32("trial_id", 193);
  w.key_u32("graph_version", 7);
  const size_t n = w.finish(false);
  REQUIRE(n > 0);

  const std::string line(buf, n);
  const std::string expect =
      R"({"t":"armed","seq":12,"req":41,"trial_id":193,"graph_version":7)";
  CHECK(line.compare(0, expect.size(), expect) == 0);
  CHECK(line.size() == expect.size() + 14);  // ,"crc":"XXXX"}
  CHECK(verify_frame(line.data(), line.size(), nullptr) == FrameError::None);

  const auto o = parse(line);
  REQUIRE(o.valid());
  uint32_t id = 0;
  CHECK(o.u32("trial_id", &id));
  CHECK(id == 193);
}

TEST_CASE("the writer round-trips every type it can emit") {
  char buf[kMaxLine];
  JsonWriter w(buf, sizeof(buf));
  w.begin("state_report", 3);
  w.key_i32("neg", -12345);
  w.key_i32("min32", INT32_MIN);
  w.key_bool("yes", true);
  w.key_bool("no", false);
  w.key_null("nothing");
  w.key_hex64("seed", 0x0123456789ABCDEFull);
  w.key_hex64("zero", 0);
  w.begin_object("timeout");
  w.key_u32("dist", 3);
  w.end_object();
  w.begin_array("word");
  w.elem_u32(1);
  w.elem_i32(-2);
  w.elem_str("timeout");
  w.end_array();
  const size_t n = w.finish(false);
  REQUIRE(n > 0);

  const auto o = parse(std::string(buf, n));
  REQUIRE(o.valid());
  int32_t i = 0;
  bool b = false;
  uint64_t h = 0;
  CHECK(o.i32("neg", &i));
  CHECK(i == -12345);
  CHECK(o.i32("min32", &i));
  CHECK(i == INT32_MIN);
  CHECK(o.boolean("yes", &b));
  CHECK(b);
  CHECK(o.boolean("no", &b));
  CHECK_FALSE(b);
  CHECK(o.is_null("nothing"));
  CHECK(o.hex64("seed", &h));
  CHECK(h == 0x0123456789ABCDEFull);
  CHECK(o.hex64("zero", &h));
  CHECK(h == 0);

  JsonObject to;
  REQUIRE(o.object("timeout", &to));
  uint8_t d = 0;
  CHECK(to.u8("dist", &d));
  CHECK(d == 3);
}

TEST_CASE("the writer emits rows the way the result path does") {
  char buf[kMaxLine];
  JsonWriter w(buf, sizeof(buf));
  w.begin("result_path", 10);
  w.key_u32("from", 0);
  w.begin_array("p");
  w.begin_elem_array();
  w.elem_u32(0);
  w.elem_str("timeout");
  w.elem_u32(255);
  w.end_array();
  w.begin_elem_array();
  w.elem_u32(1);
  w.elem_str("transition");
  w.elem_u32(2);
  w.end_array();
  w.end_array();
  const size_t n = w.finish(false);
  REQUIRE(n > 0);

  const std::string line(buf, n);
  CHECK(line.find(R"("p":[[0,"timeout",255],[1,"transition",2]])") != std::string::npos);
  CHECK(verify_frame(line.data(), line.size(), nullptr) == FrameError::None);
}

TEST_CASE("the writer emits positive signed values without a sign") {
  char buf[kMaxLine];
  JsonWriter w(buf, sizeof(buf));
  w.begin("x", 1);
  w.key_i32("pos", 12345);
  w.key_i32("zero", 0);
  w.begin_array("a");
  w.elem_i32(7);
  w.elem_i32(0);
  w.end_array();
  const size_t n = w.finish(false);
  REQUIRE(n > 0);
  const std::string line(buf, n);
  CHECK(line.find(R"("pos":12345,"zero":0,"a":[7,0])") != std::string::npos);
}

TEST_CASE("the writer escapes what must not cross the wire raw") {
  char buf[kMaxLine];
  JsonWriter w(buf, sizeof(buf));
  w.begin("log", 1);
  w.key_str("message", "he said \"no\"\n\tand\\left");
  const size_t n = w.finish(false);
  REQUIRE(n > 0);
  const std::string line(buf, n);

  // Nothing raw escaped the quoting, so the line is still one line.
  CHECK(line.find('\n') == std::string::npos);
  CHECK(line.find('\t') == std::string::npos);

  const auto o = parse(line);
  REQUIRE(o.valid());
  JsonSpan s;
  REQUIRE(o.str("message", &s));
  CHECK(json_str_eq(s, "he said \"no\"\n\tand\\left"));
}

TEST_CASE("a non-ASCII byte is escaped rather than emitted") {
  // dev/PROTOCOL.md: a byte >= 0x80 anywhere in a line is a framing error, and
  // non-ASCII text belongs in log as \uXXXX. The writer is where that is made
  // true, so a log message carrying a stray byte cannot break the link.
  char buf[kMaxLine];
  JsonWriter w(buf, sizeof(buf));
  w.begin("log", 1);
  w.key_str("message", "caf\xC3\xA9");
  const size_t n = w.finish(false);
  REQUIRE(n > 0);
  for (size_t i = 0; i < n; ++i) CHECK(static_cast<unsigned char>(buf[i]) < 0x80);
  CHECK(std::string(buf, n).find("\\u00C3\\u00A9") != std::string::npos);
}

TEST_CASE("the writer refuses to overflow rather than truncating") {
  // A message that does not fit is a firmware bug -- every message this
  // protocol defines is sized to kMaxLine by construction -- so it must not
  // produce a half-line the far end has to diagnose.
  char buf[40];
  JsonWriter w(buf, sizeof(buf));
  w.begin("result_begin", 9);
  for (int i = 0; i < 20; ++i) w.key_u32("padding", 1234567);
  CHECK(w.overflowed());
  CHECK(w.finish() == 0);
}

TEST_CASE("every json error has a message") {
  const JsonError every[] = {JsonError::None,        JsonError::NotObject,
                             JsonError::Malformed,   JsonError::TooDeep,
                             JsonError::TooManyKeys, JsonError::DuplicateKey};
  for (JsonError e : every) {
    const char* m = json_error_str(e);
    REQUIRE(m != nullptr);
    CHECK(m[0] != '\0');
  }
}
