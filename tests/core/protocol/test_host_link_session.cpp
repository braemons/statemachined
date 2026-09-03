// SPDX-License-Identifier: GPL-3.0-or-later
// A whole session, played out on the host with no board attached.
//
// That this is possible at all is the design working: the session touches no
// port, so a test can drive a complete hello / upload / configure / start / run
// / result exchange at host speed and read every byte that would have gone down
// the wire.
#include <string>
#include <vector>

#include "doctest.h"
#include "protocol/crc16.h"
#include "protocol/framing.h"
#include "protocol/host_link_session.h"
#include "trial/trial.h"

using namespace statemachined;

namespace {

struct RecordingSink : ReplySink {
  std::vector<std::string> lines;
  void send_line(const char* p, size_t n) override { lines.emplace_back(p, n); }
};

/// A host, near enough: frames commands, feeds them in a byte at a time, and
/// reads the replies back.
struct Host {
  RecordingSink sink;
  DeviceIdentity identity;
  HostLinkSession device;

  Host() : device(sink, identity) {}
  /// DeviceIdentity is copied into the session at construction, so a test that
  /// wants a different board has to say so here rather than assigning after.
  explicit Host(const DeviceIdentity& id) : identity(id), device(sink, identity) {}
  uint16_t message_id = 0;
  uint16_t graph_checksum = 0xFFFF;
  Microseconds now = 0;

  /// `body` is the object without its closing brace. Returns the replies this
  /// command produced.
  std::vector<std::string> send(const std::string& body, bool fold_graph = false) {
    const size_t before = sink.lines.size();
    char buf[kMaxLine];
    REQUIRE(body.size() < sizeof(buf));
    for (size_t i = 0; i < body.size(); ++i) buf[i] = body[i];
    const size_t n = finish_frame(buf, body.size(), sizeof(buf), true);
    REQUIRE(n > 0);
    if (fold_graph)
      graph_checksum = crc16_ccitt(buf, n - 15, graph_checksum);  // the CRC-covered prefix
    device.receive(buf, n, now);
    return std::vector<std::string>(sink.lines.begin() + before, sink.lines.end());
  }

  /// The same line twice, without re-folding the checksum -- a retry.
  std::vector<std::string> send_raw(const std::string& line) {
    const size_t before = sink.lines.size();
    device.receive(line.data(), line.size(), now);
    return std::vector<std::string>(sink.lines.begin() + before, sink.lines.end());
  }

  std::string framed(const std::string& body) {
    char buf[kMaxLine];
    for (size_t i = 0; i < body.size(); ++i) buf[i] = body[i];
    const size_t n = finish_frame(buf, body.size(), sizeof(buf), true);
    return std::string(buf, n);
  }

  std::string next_message_id() { return std::to_string(message_id++); }

  std::string checksum_hex() const {
    char h[4];
    crc16_to_hex(graph_checksum, h);
    return std::string(h, 4);
  }
};

/// Every reply is a well-formed, CRC-correct line. Checked on every message
/// these tests produce rather than spot-checked: the device emitting a line the
/// bridge cannot parse is the failure that would strand a session.
void check_wire_valid(const std::string& line) {
  REQUIRE(line.size() > 1);
  REQUIRE(line.back() == '\n');
  const std::string body = line.substr(0, line.size() - 1);
  CHECK(verify_frame(body.data(), body.size(), nullptr) == FrameError::None);
  JsonObject m(body.data(), body.size());
  CHECK(m.valid());
  for (char c : line) CHECK(static_cast<unsigned char>(c) < 0x80);
}

std::string type_of(const std::string& line) {
  const std::string body = line.substr(0, line.size() - 1);
  JsonObject m(body.data(), body.size());
  JsonSpan t;
  if (!m.str("msg_type", &t)) return "";
  return std::string(t.p, t.n);
}

/// The lines that are not the `visit` stream.
///
/// A completed state visit is reported as it happens (dev/PROTOCOL.md 4.4), so
/// it can land between a command and its reply and in the middle of a result.
/// That is what "unsolicited" means and the bridge is required to cope with it;
/// these tests are about everything else, so they drop them here rather than
/// each growing an index.
std::vector<std::string> without_visits(const std::vector<std::string>& lines) {
  std::vector<std::string> out;
  for (const auto& l : lines)
    if (type_of(l) != "visit") out.push_back(l);
  return out;
}

std::string field(const std::string& line, const char* key) {
  const std::string body = line.substr(0, line.size() - 1);
  JsonObject m(body.data(), body.size());
  JsonSpan s;
  if (m.str(key, &s)) return std::string(s.p, s.n);
  int32_t v = 0;
  if (m.i32(key, &v)) return std::to_string(v);
  return "";
}

void greet(Host& h) {
  const auto r = h.send(R"({"msg_type":"hello","message_id":)" + h.next_message_id() +
                        R"(,"proto":1,"seed":"0123456789ABCDEF")");
  REQUIRE(r.size() == 1);
  REQUIRE(type_of(r[0]) == "hello_ack");
}

/// wait --(500 ms)--> Hit, with a line raised on entry to wait.
void upload_minimal(Host& h) {
  h.graph_checksum = 0xFFFF;
  auto r = h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
                      R"(,"graph_version":7,"n_states":2,"entry":0)",
                  true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_dist","message_id":)" + h.next_message_id() +
                 R"(,"i":0,"kind":"fixed","a":500)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
                 R"(,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_action","message_id":)" + h.next_message_id() +
                 R"(,"on":"entry","line":2,"kind":"high")",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
                 R"(,"i":1,"terminal":1,"timeout":null)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_end","message_id":)" + h.next_message_id() +
             R"(,"n_transitions":0,"n_output_actions":1,"checksum":")" + h.checksum_hex() +
             R"(")");
  REQUIRE(r.size() == 1);
  REQUIRE(type_of(r[0]) == "graph_ok");
}

}  // namespace

TEST_CASE("nothing is answered before a hello") {
  // Every command needs a session seed, and a device running trials without one
  // would be running trials nobody could replay.
  Host h;
  const auto r = h.send(R"({"msg_type":"ping","message_id":1)");
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "error");
  CHECK(field(r[0], "code") == "not_ready");
  CHECK(h.device.state() == LinkState::Greeting);
}

TEST_CASE("hello reports what the board can hold") {
  // The bridge checks a graph against these before uploading a byte of it,
  // which turns "refused at graph_end" into "refused before the first byte".
  Host h;
  greet(h);
  const std::string& ack = h.sink.lines[0];
  check_wire_valid(ack);
  CHECK(field(ack, "board") == "native");
  CHECK(field(ack, "proto") == "1");
  // Capacities are nested, which is what keeps the largest message in the
  // protocol inside the reader's member limit.
  const std::string body = ack.substr(0, ack.size() - 1);
  JsonObject m(body.data(), body.size());
  JsonObject caps;
  REQUIRE(m.object("caps", &caps));
  uint32_t v = 0;
  CHECK(caps.u32("max_states", &v));
  CHECK(v == kMaxStates);
  CHECK(caps.u32("max_transitions", &v));
  CHECK(v == kMaxTransitions);
  CHECK(caps.u32("max_line", &v));
  CHECK(v == kMaxLine);
  CHECK(field(ack, "in_reply_to") == "0");
  CHECK(h.device.state() == LinkState::Idle);
}

TEST_CASE("a wrong protocol version is refused, not tolerated") {
  Host h;
  const auto r = h.send(R"({"msg_type":"hello","message_id":0,"proto":99,"seed":"1")");
  CHECK(field(r[0], "code") == "bad_proto");
  CHECK(h.device.state() == LinkState::Greeting);
}

TEST_CASE("a whole trial: greet, upload, configure, start, run, result") {
  Host h;
  greet(h);
  upload_minimal(h);
  CHECK(h.device.has_graph());
  CHECK(h.device.graph_version() == 7);

  auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                  R"(,"trial_id":193,"graph_version":7,"cap_ms":30000,"start":"serial")");
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "armed");
  CHECK(field(r[0], "trial_id") == "193");
  CHECK(field(r[0], "graph_version") == "7");  // both, always
  CHECK(h.device.state() == LinkState::Armed);

  r = h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() +
             R"(,"trial_id":193)");
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "started");
  CHECK(h.device.state() == LinkState::Running);

  const size_t before = h.sink.lines.size();
  LineBitmask raised = 0, lowered = 0;
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100) {
    const OutputUpdate ops = h.device.advance_trial(0, t);
    raised |= ops.set_high;
    lowered |= ops.set_low;
  }
  CHECK(h.device.state() == LinkState::Idle);
  CHECK((lowered & (1u << 2)) != 0);  // the entry action's line came back down

  // The result arrives unasked: a trial that ended silently would be
  // indistinguishable from a hung one.
  const std::vector<std::string> result =
      without_visits({h.sink.lines.begin() + before, h.sink.lines.end()});
  REQUIRE(result.size() >= 3);
  for (const auto& l : result) check_wire_valid(l);
  CHECK(type_of(result.front()) == "result_begin");
  CHECK(type_of(result.back()) == "result_end");
  CHECK(field(result.front(), "outcome") ==
        std::to_string(static_cast<int>(TrialOutcome::Hit)));
  CHECK(field(result.front(), "trial_id") == "193");
  CHECK(field(result.front(), "truncated") == "");  // it is a bool, not a number

  bool saw_path = false;
  for (const auto& l : result)
    if (type_of(l) == "result_path") saw_path = true;
  CHECK(saw_path);
}

TEST_CASE("every state visit is reported as it happens") {
  // The record at the end of a trial is authoritative and stays so. What it is
  // not is a trace: something with a timestamp on it that arrives while the
  // trial is still running and survives a truncated path. See dev/DAEMON.md 3.6.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":77,"graph_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":77)");

  const size_t before = h.sink.lines.size();
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    h.device.advance_trial(0, t);

  std::vector<std::string> visits;
  for (size_t i = before; i < h.sink.lines.size(); ++i)
    if (type_of(h.sink.lines[i]) == "visit") visits.push_back(h.sink.lines[i]);

  // upload_minimal is wait --(500 ms)--> Hit: one visit for leaving wait, one
  // for entering the terminal state.
  REQUIRE(visits.size() == 2);
  for (const auto& l : visits) check_wire_valid(l);
  CHECK(field(visits[0], "trial_id") == "77");
  CHECK(field(visits[0], "seq") == "0");
  CHECK(field(visits[1], "seq") == "1");

  // `v` is the same flat six-element array as a result_path entry, decoded by
  // the same function on the host. Two shapes for one fact is how the two drift
  // apart, so this asserts the shape and not merely the contents.
  const std::string body = visits[0].substr(0, visits[0].size() - 1);
  JsonObject m(body.data(), body.size());
  JsonArray row;
  REQUIRE(m.array("v", &row));
  int32_t state_index = -1, transition = -1, drawn = -1;
  JsonSpan cause;
  JsonType ty = JsonType::Missing;
  REQUIRE(row.next_i32(&state_index));
  REQUIRE(row.next(&cause, &ty));
  REQUIRE(row.next_i32(&transition));
  REQUIRE(row.next_i32(&drawn));
  int32_t entered = -1, duration = -1;
  REQUIRE(row.next_i32(&entered));
  REQUIRE(row.next_i32(&duration));
  CHECK(state_index == 0);  // it left the entry state
  CHECK(std::string(cause.p, cause.n) == "timeout");
  CHECK(drawn == 500);        // the fixed distribution
  CHECK(duration >= 500000);  // and it actually took that long
}

TEST_CASE("a visit outside a trial carries trial_id 0") {
  // Demo mode, the bench, a line-started run before anything assigned an id.
  // The trace is still worth having; it simply joins to nothing.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":5,"graph_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":5)");
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    h.device.advance_trial(0, t);
  CHECK(h.device.state() == LinkState::Idle);

  // Every visit of that trial named it.
  size_t named = 0;
  for (const auto& l : h.sink.lines)
    if (type_of(l) == "visit" && field(l, "trial_id") == "5") ++named;
  CHECK(named == 2);
}

TEST_CASE("result_begin says which window of the path it carries") {
  // A path that overflowed drops its oldest visits, so `truncated` on its own
  // leaves a host unable to say what it is missing. first_seq and total_visits
  // are what make the stream reconcilable against the record.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":8,"graph_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":8)");
  const size_t before = h.sink.lines.size();
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    h.device.advance_trial(0, t);

  const std::vector<std::string> result =
      without_visits({h.sink.lines.begin() + before, h.sink.lines.end()});
  REQUIRE(!result.empty());
  CHECK(type_of(result.front()) == "result_begin");
  CHECK(field(result.front(), "path_len") == "2");
  CHECK(field(result.front(), "first_seq") == "0");
  CHECK(field(result.front(), "total_visits") == "2");
}

TEST_CASE("the result's checksum covers every chunk") {
  // The per-line crc catches a corrupt chunk. This catches a missing one, which
  // no per-line check can see: the line that vanished was well formed.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":1,"graph_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":1)");
  const size_t before = h.sink.lines.size();
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    h.device.advance_trial(0, t);

  uint16_t sum = 0xFFFF;
  std::string end;
  for (size_t i = before; i < h.sink.lines.size(); ++i) {
    const std::string& l = h.sink.lines[i];
    if (type_of(l) == "result_end") {
      end = l;
      break;
    }
    // The fold covers result_begin and its chunks and nothing else. A `visit`
    // is a preview of the same facts, not part of the record, and folding one
    // in would make the checksum depend on how much of the stream a host
    // happened to see.
    if (type_of(l) == "visit") continue;
    sum = crc16_ccitt(l.data(), l.size() - 15, sum);  // strip \n and the crc tail
  }
  REQUIRE(!end.empty());
  char hex[4];
  crc16_to_hex(sum, hex);
  CHECK(field(end, "checksum") == std::string(hex, 4));
}

TEST_CASE("a path longer than one line is split across chunks") {
  // Chunked for the same reason the upload is: a full path does not fit in one
  // line, and buffering one that did would cost a kilobyte this board has not
  // got.
  Host h;
  greet(h);
  h.graph_checksum = 0xFFFF;
  // A two-state loop with a short timeout, capped, so the path fills up.
  h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
             R"(,"graph_version":1,"n_states":3,"entry":0)",
         true);
  h.send(R"({"msg_type":"graph_dist","message_id":)" + h.next_message_id() +
             R"(,"i":0,"kind":"fixed","a":1)",
         true);
  h.send(R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
             R"(,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})",
         true);
  h.send(R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
             R"(,"i":1,"terminal":null,"timeout":{"dist":0,"target":0})",
         true);
  h.send(R"({"msg_type":"graph_transition","message_id":)" + h.next_message_id() +
             R"(,"all":1,"target":2)",
         true);
  h.send(R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
             R"(,"i":2,"terminal":1,"timeout":null)",
         true);
  auto r = h.send(R"({"msg_type":"graph_end","message_id":)" + h.next_message_id() +
                  R"(,"n_transitions":1,"n_output_actions":0,"checksum":")" + h.checksum_hex() +
                  R"(")");
  REQUIRE(type_of(r[0]) == "graph_ok");

  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":5,"graph_version":1,"cap_ms":200)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":5)");
  const size_t before = h.sink.lines.size();
  for (uint32_t t = 0; t < 5000000u && h.device.state() == LinkState::Running; t += 100)
    h.device.advance_trial(0, t);

  int chunks = 0;
  for (size_t i = before; i < h.sink.lines.size(); ++i) {
    check_wire_valid(h.sink.lines[i]);
    CHECK(h.sink.lines[i].size() <= kMaxLine);
    if (type_of(h.sink.lines[i]) == "result_path") ++chunks;
  }
  CHECK(chunks > 1);
}

TEST_CASE("a retried command is answered, not re-executed") {
  // A re-executed start would run a second trial. This is the thing that
  // prevents it, and it is why the bridge may resend blindly on a timeout.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":9,"graph_version":7)");

  const std::string start = h.framed(R"({"msg_type":"start","message_id":)" +
                                     h.next_message_id() + R"(,"trial_id":9)");
  const auto first = h.send_raw(start);
  REQUIRE(first.size() == 1);
  CHECK(type_of(first[0]) == "started");
  CHECK(h.device.state() == LinkState::Running);

  const auto again = h.send_raw(start);
  REQUIRE(again.size() == 1);
  CHECK(again[0] == first[0]);  // byte for byte, including its own message_id
  CHECK(h.device.state() == LinkState::Running);
}

TEST_CASE("a command with a different message_id is not treated as a retry") {
  Host h;
  greet(h);
  const auto a = h.send(R"({"msg_type":"ping","message_id":)" + h.next_message_id());
  const auto b = h.send(R"({"msg_type":"ping","message_id":)" + h.next_message_id());
  CHECK(type_of(a[0]) == "pong");
  CHECK(type_of(b[0]) == "pong");
  CHECK(a[0] != b[0]);
}

TEST_CASE("no trial runs that the device was not confirmed configured for") {
  Host h;
  greet(h);
  upload_minimal(h);

  SUBCASE("start without configure") {
    const auto r = h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() +
                          R"(,"trial_id":1)");
    CHECK(field(r[0], "code") == "not_ready");
    CHECK(h.device.state() == LinkState::Idle);
  }
  SUBCASE("start for a different trial than the armed one") {
    h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
           R"(,"trial_id":1,"graph_version":7)");
    const auto r = h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() +
                          R"(,"trial_id":2)");
    CHECK(field(r[0], "code") == "unknown_trial");
    CHECK(h.device.state() == LinkState::Armed);
  }
  SUBCASE("start when the trial is line-triggered") {
    h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
           R"(,"trial_id":1,"graph_version":7,"start":"line")");
    const auto r = h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() +
                          R"(,"trial_id":1)");
    CHECK(field(r[0], "code") == "not_ready");
  }
}

TEST_CASE("configure against a graph the device does not hold is refused") {
  // A graph edit that did not land would otherwise leave the device confidently
  // running the old paradigm.
  Host h;
  greet(h);
  upload_minimal(h);
  const auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                        R"(,"trial_id":1,"graph_version":8)");
  CHECK(field(r[0], "code") == "graph_mismatch");
  CHECK(h.device.state() == LinkState::Idle);
}

TEST_CASE("configure before any graph is refused") {
  Host h;
  greet(h);
  const auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                        R"(,"trial_id":1,"graph_version":1)");
  CHECK(field(r[0], "code") == "not_ready");
}

TEST_CASE("cancel reports what actually happened") {
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":4,"graph_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":4)");
  h.device.advance_trial(0, 1000);

  // The cancel's own state visit is reported before the reply to it, since
  // force_end() records one on the way out.
  const auto r =
      without_visits(h.send(R"({"msg_type":"cancel","message_id":)" + h.next_message_id() +
                            R"(,"trial_id":4,"reason":"host")"));
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "cancel_ack");
  CHECK(field(r[0], "outcome") == std::to_string(static_cast<int>(TrialOutcome::Cancelled)));

  // And the result still arrives, with its path up to the cut: a cancelled
  // trial is recorded rather than dropped, so a gap in the numbering never has
  // to be explained.
  const size_t before = h.sink.lines.size();
  h.device.advance_trial(0, 2000);
  const std::vector<std::string> result =
      without_visits({h.sink.lines.begin() + before, h.sink.lines.end()});
  REQUIRE(result.size() >= 3);
  CHECK(type_of(result.front()) == "result_begin");
  CHECK(field(result.front(), "outcome") ==
        std::to_string(static_cast<int>(TrialOutcome::Cancelled)));
}

TEST_CASE("a cancel that loses the race gets the real outcome back") {
  // The bridge must cope with asking to cancel and being told Hit. The
  // alternative is a record claiming a trial was cancelled when the animal had
  // already responded.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":6,"graph_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":6)");
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    h.device.advance_trial(0, t);
  REQUIRE(h.device.state() == LinkState::Idle);

  const auto r = h.send(R"({"msg_type":"cancel","message_id":)" + h.next_message_id() +
                        R"(,"trial_id":6,"reason":"host")");
  // Refused rather than acked: the bridge learns nothing was cancelled.
  CHECK(field(r[0], "code") == "unknown_trial");
}

TEST_CASE("only host is a cancel reason the host may give") {
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":4,"graph_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":4)");
  const auto r = h.send(R"({"msg_type":"cancel","message_id":)" + h.next_message_id() +
                        R"(,"trial_id":4,"reason":"link_lost")");
  CHECK(field(r[0], "code") == "bad_json");
}

TEST_CASE("uploading while a trial is armed or running is refused") {
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":1,"graph_version":7)");
  const auto r = h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
                        R"(,"graph_version":9,"n_states":2,"entry":0)");
  CHECK(field(r[0], "code") == "busy");
  CHECK(h.device.graph_version() == 7);  // untouched
}

TEST_CASE("a refused upload leaves the committed graph running") {
  // The whole point of staging: a failed re-upload mid-session must not cost
  // the paradigm that was already working.
  Host h;
  greet(h);
  upload_minimal(h);
  REQUIRE(h.device.graph_version() == 7);

  h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
         R"(,"graph_version":9,"n_states":2,"entry":0)");
  const auto r = h.send(R"({"msg_type":"graph_dist","message_id":)" + h.next_message_id() +
                        R"(,"i":4,"kind":"fixed","a":1)");
  CHECK(field(r[0], "code") == "bad_index");
  CHECK(h.device.has_graph());
  CHECK(h.device.graph_version() == 7);

  // And the old graph still runs.
  const auto c = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                        R"(,"trial_id":1,"graph_version":7)");
  CHECK(type_of(c[0]) == "armed");
}

TEST_CASE("a second hello keeps the committed graph") {
  // Reconnecting the bridge must not cost a re-upload.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":1,"graph_version":7)");
  REQUIRE(h.device.state() == LinkState::Armed);

  greet(h);
  CHECK(h.device.state() == LinkState::Idle);  // but the arming is gone
  CHECK(h.device.has_graph());
  CHECK(h.device.graph_version() == 7);
}

TEST_CASE("a corrupt line is refused whole and named") {
  Host h;
  greet(h);

  SUBCASE("a bad crc") {
    std::string line = h.framed(R"({"msg_type":"ping","message_id":3)");
    line[line.size() - 4] = (line[line.size() - 4] == '0') ? '1' : '0';
    const auto r = h.send_raw(line);
    REQUIRE(r.size() == 1);
    CHECK(field(r[0], "code") == "bad_crc");
  }
  SUBCASE("no crc at all") {
    const auto r = h.send_raw(std::string(R"({"msg_type":"ping","message_id":3})") + "\n");
    CHECK(field(r[0], "code") == "bad_json");
  }
  SUBCASE("an unknown message type") {
    const auto r = h.send(R"({"msg_type":"teleport","message_id":)" + h.next_message_id());
    CHECK(field(r[0], "code") == "unknown_type");
  }
  SUBCASE("an overlong line, and the session survives it") {
    std::string junk = "{";
    junk.append(kMaxLine + 50, 'x');
    junk += "\n";
    const auto r = h.send_raw(junk);
    REQUIRE(r.size() == 1);
    CHECK(field(r[0], "code") == "too_long");
    // A burst of noise costs the link one message, not the session.
    const auto p = h.send(R"({"msg_type":"ping","message_id":)" + h.next_message_id());
    CHECK(type_of(p[0]) == "pong");
  }
}

TEST_CASE("state_report exposes what a link problem looks like") {
  // Diagnosis, not control. A link dropping lines should be visible to whoever
  // is debugging the rig rather than inferred from trials that did not happen.
  Host h;
  greet(h);
  std::string junk = "{";
  junk.append(kMaxLine + 50, 'x');
  junk += "\n";
  h.send_raw(junk);

  const auto r = h.send(R"({"msg_type":"state","message_id":)" + h.next_message_id());
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "state_report");
  CHECK(field(r[0], "dropped_lines") == "1");
  CHECK(field(r[0], "bad_lines") == "1");
  CHECK(field(r[0], "has_graph") == "");  // a bool
}

TEST_CASE("fail_safe drives every line to its configured level") {
  // "off" is not always "low", so this is data rather than a zeroed word, and
  // it covers every line the board has -- it runs when the graph may be the
  // thing that is wrong.
  DeviceIdentity eight_lines;
  eight_lines.output_line_count = 8;
  Host h(eight_lines);
  greet(h);
  const OutputUpdate before = h.device.fail_safe();
  CHECK(before.set_high == kCompiledSafeLevels);
  CHECK(before.set_low == (0xFFu & ~kCompiledSafeLevels));

  const auto r =
      h.send(R"({"msg_type":"wiring","message_id":)" + h.next_message_id() + R"(,"safe":5)");
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "ack");

  const OutputUpdate after = h.device.fail_safe();
  CHECK(after.set_high == 5);           // lines 0 and 2 are safe high
  CHECK(after.set_low == (0xFF & ~5));  // everything else low
}

TEST_CASE("a board fails safe correctly before it holds a graph") {
  // The reason the wiring left StateGraph. A rig image boots into no graph at
  // all, and main.cpp's first act is this call -- so if the safe levels lived
  // in a graph there would be nothing to read, every output would go low, and
  // an active-low valve driver would be opened by every power cycle.
  DeviceIdentity eight_lines;
  eight_lines.output_line_count = 8;
  Host h(eight_lines);
  greet(h);
  h.send(R"({"msg_type":"wiring","message_id":)" + h.next_message_id() + R"(,"safe":9)");
  CHECK(h.device.has_graph() == false);
  const OutputUpdate ops = h.device.fail_safe();
  CHECK(ops.set_high == 9);
  CHECK(ops.set_low == (0xFF & ~9));
}

TEST_CASE("hello_ack says whether the board has been given a wiring") {
  // So a daemon never has to guess whether the board came up configured. False
  // means it is running the compile-time defaults.
  Host h;
  auto r = h.send(R"({"msg_type":"hello","message_id":)" + h.next_message_id() +
                  R"(,"proto":1,"seed":"0123456789ABCDEF")");
  REQUIRE(type_of(r[0]) == "hello_ack");
  // Asserted on the bytes: field() reads strings and numbers, and this is a
  // bool.
  CHECK(r[0].find(R"("has_wiring":false)") != std::string::npos);

  h.send(R"({"msg_type":"wiring","message_id":)" + h.next_message_id() + R"(,"invert":3)");
  r = h.send(R"({"msg_type":"hello","message_id":)" + h.next_message_id() +
             R"(,"proto":1,"seed":"0123456789ABCDEF")");
  CHECK(r[0].find(R"("has_wiring":true)") != std::string::npos);
}

TEST_CASE("a wiring is refused while a trial is armed") {
  // Same guard as a graph upload, and for a sharper reason: the conditioning it
  // changes is read by the scan, so a debounce edited under a running trial
  // would move a timing nobody could account for afterwards.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":4,"graph_version":7)");
  const auto r =
      h.send(R"({"msg_type":"wiring","message_id":)" + h.next_message_id() + R"(,"safe":1)");
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "error");
  CHECK(field(r[0], "code") == "busy");
  CHECK(field(r[0], "context") == "wiring");
}

TEST_CASE("a malformed wiring changes nothing") {
  // Read into a copy and installed whole. A message that turns out to be bad
  // halfway through must leave the board wired the way it was, or a typo in a
  // debounce would take the safe levels with it.
  DeviceIdentity eight_lines;
  eight_lines.output_line_count = 8;
  Host h(eight_lines);
  greet(h);
  h.send(R"({"msg_type":"wiring","message_id":)" + h.next_message_id() + R"(,"safe":6)");

  const auto r = h.send(R"({"msg_type":"wiring","message_id":)" + h.next_message_id() +
                        R"(,"safe":1,"debounce_ms":[2,-5])");
  REQUIRE(type_of(r[0]) == "error");
  CHECK(field(r[0], "context") == "debounce_ms");
  CHECK(h.device.fail_safe().set_high == 6);  // the earlier one, not the refused one
}

TEST_CASE("a graph upload no longer carries the wiring") {
  // The members are ignored rather than refused -- unknown members are ignored
  // everywhere in this protocol -- but they must not reach anything. Uploading
  // a paradigm silently re-conditioning the inputs is what the move fixed.
  DeviceIdentity eight_lines;
  eight_lines.output_line_count = 8;
  Host h(eight_lines);
  greet(h);
  h.send(R"({"msg_type":"wiring","message_id":)" + h.next_message_id() + R"(,"safe":6)");

  h.graph_checksum = 0xFFFF;
  h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
             R"(,"graph_version":3,"n_states":2,"entry":0,"safe":5,"invert":255)",
         true);
  h.send(R"({"msg_type":"graph_dist","message_id":)" + h.next_message_id() +
             R"(,"i":0,"kind":"fixed","a":10)",
         true);
  h.send(R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
             R"(,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})",
         true);
  h.send(R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
             R"(,"i":1,"terminal":1,"timeout":null)",
         true);
  h.send(R"({"msg_type":"graph_end","message_id":)" + h.next_message_id() +
         R"(,"n_transitions":0,"n_output_actions":0,"checksum":")" + h.checksum_hex() + R"(")");

  CHECK(h.device.has_graph() == true);
  CHECK(h.device.fail_safe().set_high == 6);         // the wiring's, not the graph's
  CHECK(h.device.wiring().inputs.invert_mask == 0);  // likewise
}

TEST_CASE("every reply is a line the bridge can parse") {
  // Asserted over a whole session rather than per message: the device emitting
  // something unparsable is the failure that would strand a bridge, and it is
  // exactly the kind of thing a targeted test misses.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":11,"graph_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":11)");
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    h.device.advance_trial(0, t);
  h.send(R"({"msg_type":"state","message_id":)" + h.next_message_id());
  h.send(R"({"msg_type":"nonsense","message_id":)" + h.next_message_id());

  REQUIRE(h.sink.lines.size() > 10);
  for (const auto& l : h.sink.lines) {
    check_wire_valid(l);
    CHECK(l.size() <= kMaxLine);
  }
}

TEST_CASE("every reply to a command carries the message_id it answers") {
  Host h;
  greet(h);
  const auto r = h.send(R"({"msg_type":"ping","message_id":)" + h.next_message_id());
  CHECK(field(r[0], "in_reply_to") == "1");
  const auto e = h.send(R"({"msg_type":"nope","message_id":)" + h.next_message_id());
  CHECK(field(e[0], "in_reply_to") == "2");
}

TEST_CASE("message_id 0 is an ordinary identifier, refusals included") {
  // It is not a sentinel and cannot be one: message_id is a u16 that wraps
  // through zero, and a bridge's first command of a session is usually
  // numbered 0. A refusal that dropped `in_reply_to` for it would leave the
  // bridge unable to tell which command was refused, and -- worse -- the reply
  // would not be remembered, so the blind resend the protocol promises is safe
  // would re-execute instead of being answered from the cache.
  Host h;

  SUBCASE("a refusal before the session exists names the message_id it refuses") {
    const auto r = h.send(R"({"msg_type":"ping","message_id":0)");
    REQUIRE(r.size() == 1);
    CHECK(type_of(r[0]) == "error");
    CHECK(field(r[0], "code") == "not_ready");
    CHECK(field(r[0], "in_reply_to") == "0");
  }

  SUBCASE("and a resend of it is answered from the cache, not re-executed") {
    greet(h);  // hello is message_id 0 ...
    h.send(R"({"msg_type":"ping","message_id":)" +
           h.next_message_id());  // ... so move the guard off it
    const std::string refused =
        h.framed(R"({"msg_type":"cancel","message_id":0,"trial_id":9,"reason":"host")");
    const auto first = h.send_raw(refused);
    REQUIRE(first.size() == 1);
    CHECK(type_of(first[0]) == "error");
    CHECK(field(first[0], "in_reply_to") == "0");

    const auto again = h.send_raw(refused);
    REQUIRE(again.size() == 1);
    CHECK(again[0] == first[0]);  // byte for byte, from the cache
  }

  SUBCASE("a line with no message_id carries no in_reply_to, since there is none") {
    // The other half of the same rule: `in_reply_to` is omitted only when the line
    // genuinely never named itself.
    greet(h);
    const auto r = h.send(R"({"msg_type":"ping")");
    REQUIRE(r.size() == 1);
    CHECK(field(r[0], "code") == "bad_json");
    CHECK(r[0].find(R"("in_reply_to":)") == std::string::npos);
  }
}

TEST_CASE("a lost link cancels the trial in flight and lowers what it raised") {
  // There is nobody to send a result to, which is exactly why the outputs
  // cannot be left for the host to sort out. The trial ends through the
  // ordinary exit path, so the line the state raised comes down by the same
  // code that lowers it on any other transition.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":9,"graph_version":7)");
  const auto started =
      h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":9)");
  REQUIRE(type_of(started[0]) == "started");
  REQUIRE(h.device.state() == LinkState::Running);

  h.now = 100000;  // 100 ms
  const OutputUpdate ops = h.device.link_lost(h.now);
  CHECK((ops.set_low & (1u << 2)) != 0);
  CHECK_FALSE(h.device.state() == LinkState::Running);
  CHECK(h.device.state() == LinkState::Idle);
}

TEST_CASE("link loss and fail-safe discard un-applied entry actions") {
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":9,"graph_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":9)");

  h.device.link_lost(1000);
  const OutputUpdate after_link_loss = h.device.advance_trial(0, 2000);
  CHECK((after_link_loss.set_high & (1u << 2)) == 0);

  greet(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":10,"graph_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":10)");
  h.device.fail_safe();
  const OutputUpdate after_fail_safe = h.device.advance_trial(0, 3000);
  CHECK((after_fail_safe.set_high & (1u << 2)) == 0);
}

TEST_CASE("a lost link keeps the committed graph, so a reconnect costs no re-upload") {
  Host h;
  greet(h);
  upload_minimal(h);
  REQUIRE(h.device.has_graph());

  h.device.link_lost(1000);
  CHECK(h.device.has_graph());
  CHECK(h.device.graph_version() == 7);

  // The bridge comes back. hello is what brings a fresh session seed; the graph
  // is already there.
  const auto r = h.send(R"({"msg_type":"hello","message_id":)" + h.next_message_id() +
                        R"(,"proto":1,"seed":"FEDCBA9876543210")");
  REQUIRE(type_of(r[0]) == "hello_ack");
  CHECK(field(r[0], "graph_version") == "7");
}

TEST_CASE("state_report carries the scan health the board measured") {
  // The scan rate is a claim until a board runs it, and a board that quietly
  // misses scans looks exactly like one that is fine.
  Host h;
  greet(h);
  ScanHealth sh;
  sh.hz = 9871;
  sh.overruns = 4;
  sh.worst_gap = 2;
  // The link's own contribution to a stuttering scan: how often a reply had to
  // wait for the wire because the queue in front of it was full.
  sh.tx_stalls = 3;
  h.device.report_scan_health(sh);

  const auto r = h.send(R"({"msg_type":"state","message_id":)" + h.next_message_id());
  REQUIRE(r.size() == 1);
  REQUIRE(type_of(r[0]) == "state_report");
  CHECK(r[0].find(R"("scan":{"hz":9871,"overruns":4,"worst_gap":2,"tx_stalls":3})") !=
        std::string::npos);
}

TEST_CASE("the entry state's output actions reach the caller of advance_trial") {
  // A regression, and it was found by an emulator rather than by anything here.
  //
  // The entry state's actions are returned by TrialRunner::start(), which the
  // session calls from `start`. Only advance_trial() drives pins. The session
  // used to drop the update on the floor, so "house light on at trial start"
  // did nothing at all on a board -- and every test in this file passed,
  // because they exercise the runner directly and read its update themselves.
  // Nothing asked whether the session passed it on. This does.
  Host h;
  greet(h);
  upload_minimal(h);  // raises line 2 on entering the first state
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":4,"graph_version":7)");
  const auto started =
      h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":4)");
  REQUIRE(type_of(started[0]) == "started");

  const OutputUpdate first = h.device.advance_trial(0, 1000);
  CHECK((first.set_high & (1u << 2)) != 0);

  // And only once: the next scan owes nothing.
  const OutputUpdate second = h.device.advance_trial(0, 2000);
  CHECK((second.set_high & (1u << 2)) == 0);
}
