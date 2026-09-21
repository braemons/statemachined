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

  /// Open a set upload. Every graph upload is inside one now, so this is the
  /// line that precedes them all.
  std::vector<std::string> open_set(int version, int n_graphs = 1) {
    graph_checksum = 0xFFFF;
    return send(R"({"msg_type":"set_begin","message_id":)" + next_message_id() +
                    R"(,"set_version":)" + std::to_string(version) + R"(,"n_graphs":)" +
                    std::to_string(n_graphs),
                true);
  }

  /// Close it. The checksum is over everything folded since open_set, so this
  /// message is the one that carries it and the one that is not folded.
  std::vector<std::string> close_set(int n_states, int n_transitions, int n_actions) {
    return send(R"({"msg_type":"set_end","message_id":)" + next_message_id() +
                R"(,"n_states":)" + std::to_string(n_states) + R"(,"n_transitions":)" +
                std::to_string(n_transitions) + R"(,"n_output_actions":)" +
                std::to_string(n_actions) + R"(,"checksum":")" + checksum_hex() + R"(")");
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
/// A completed state visit is reported as it happens (docs/reference/protocol.md 4.4), so
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

/// advance_trial() and the drain every real caller pairs with it.
///
/// On a board those are deliberately in different contexts -- the trial loop is
/// the scan ISR, the formatting is the foreground -- so the session hands out
/// two calls rather than one. A test that made only the first would see a
/// silent visit stream and conclude the wrong thing.
OutputUpdate advance(Host& h, LineBitmask word, Microseconds now) {
  const OutputUpdate ops = h.device.advance_trial(word, now);
  h.device.drain_outbound();
  return ops;
}

void greet(Host& h) {
  const auto r = h.send(R"({"msg_type":"hello","message_id":)" + h.next_message_id() +
                        R"(,"proto":1,"seed":"0123456789ABCDEF")");
  REQUIRE(r.size() == 1);
  REQUIRE(type_of(r[0]) == "hello_ack");
}

/// wait --(500 ms)--> Hit, with a line raised on entry to wait.
void upload_minimal(Host& h) {
  auto r = h.open_set(7);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
                 R"(,"slot":0,"n_states":2,"entry":0)",
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
                 R"(,"n_transitions":0,"n_output_actions":1)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.close_set(2, 0, 1);
  REQUIRE(r.size() == 1);
  REQUIRE(type_of(r[0]) == "set_ok");
}

/// The same graph, with the terminal state declaring a 200 ms dwell -- the
/// inter-trial interval a self-driving board waits out before starting another
/// run.
void upload_relighting(Host& h) {
  auto r = h.open_set(7);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
                 R"(,"slot":0,"n_states":2,"entry":0)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_dist","message_id":)" + h.next_message_id() +
                 R"(,"i":0,"kind":"fixed","a":500)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_dist","message_id":)" + h.next_message_id() +
                 R"(,"i":1,"kind":"fixed","a":200)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
                 R"(,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
                 R"(,"i":1,"terminal":1,"timeout":null,"relight":1)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_end","message_id":)" + h.next_message_id() +
                 R"(,"n_transitions":0,"n_output_actions":0)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.close_set(2, 0, 0);
  REQUIRE(type_of(r[0]) == "set_ok");
}

constexpr Microseconds ms(uint32_t n) { return n * 1000u; }

/// A set whose one graph runs `wait --(100 ms)--> done`, where `done` starts
/// global timer 0 on entry. The timer waits 200 ms, then holds output line 0
/// high for 100 ms -- an edge that lands well after the trial has ended, which
/// is the point.
void upload_with_a_timer(Host& h) {
  auto r = h.open_set(7);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
                 R"(,"slot":0,"n_states":2,"entry":0)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  // 0: the state's own dwell. 1: the timer's onset delay. 2: its width.
  r = h.send(R"({"msg_type":"graph_dist","message_id":)" + h.next_message_id() +
                 R"(,"i":0,"kind":"fixed","a":100)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_dist","message_id":)" + h.next_message_id() +
                 R"(,"i":1,"kind":"fixed","a":200)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_dist","message_id":)" + h.next_message_id() +
                 R"(,"i":2,"kind":"fixed","a":100)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_timer","message_id":)" + h.next_message_id() +
                 R"(,"i":0,"delay":1,"width":2,"line":0)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
                 R"(,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
                 R"(,"i":1,"terminal":1,"timeout":null)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  // On entering the terminal state: start the timer. A pseudo-output, Bpod's
  // way, so there is one action vocabulary rather than two.
  r = h.send(R"({"msg_type":"graph_action","message_id":)" + h.next_message_id() +
                 R"(,"on":"entry","kind":"timer_start","timer":0)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_end","message_id":)" + h.next_message_id() +
                 R"(,"n_transitions":0,"n_output_actions":1)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.close_set(2, 0, 1);
  REQUIRE(type_of(r[0]) == "set_ok");
}

/// A boolean member, which `field` cannot report: it renders numbers and
/// strings, and `true` is neither.
bool flag(const std::string& line, const char* key) {
  const std::string body = line.substr(0, line.size() - 1);
  JsonObject m(body.data(), body.size());
  bool v = false;
  return m.boolean(key, &v) && v;
}

/// Scan for `us` of device time, in 1 ms steps, the way the timer would.
void scan_for(Host& h, Microseconds us) {
  const Microseconds until = h.now + us;
  while (h.now < until) {
    h.now += 1000;
    advance(h, 0, h.now);
  }
}

/// The trial ids of every result the device has emitted.
std::vector<int> results(const RecordingSink& sink) {
  std::vector<int> ids;
  for (const auto& l : sink.lines)
    if (type_of(l) == "result_begin") ids.push_back(std::stoi(field(l, "trial_id")));
  return ids;
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
  CHECK(h.device.has_set());
  CHECK(h.device.set_version() == 7);

  auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                  R"(,"trial_id":193,"set_version":7,"cap_ms":30000,"start":"serial")");
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "armed");
  CHECK(field(r[0], "trial_id") == "193");
  CHECK(field(r[0], "set_version") == "7");  // both, always
  CHECK(h.device.state() == LinkState::Armed);

  r = h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() +
             R"(,"trial_id":193)");
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "started");
  CHECK(h.device.state() == LinkState::Running);

  const size_t before = h.sink.lines.size();
  LineBitmask raised = 0, lowered = 0;
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100) {
    const OutputUpdate ops = advance(h, 0, t);
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

/// Send one upload message and insist it was accepted, naming the reply when
/// it was not: a silent failure mid-set shows up only as "no upload is open" at
/// set_end, which says nothing about which message the host got wrong.
void ack_or_die(Host& h, const std::string& body, bool fold) {
  const auto r = h.send(body, fold);
  REQUIRE(r.size() == 1);
  INFO(r[0]);
  REQUIRE(type_of(r[0]) == "ack");
}

/// Two graphs in one set: slot 0 ends after 500 ms, slot 1 after 100 ms. Their
/// terminal outcomes differ too, so a test can tell from the result which one
/// actually ran rather than from a duration it might have got by accident.
void upload_two(Host& h) {
  auto r = h.open_set(12, 2);
  REQUIRE(type_of(r[0]) == "ack");

  // The shared pool is filled first, at set level: both graphs draw from it.
  ack_or_die(h,
             R"({"msg_type":"graph_dist","message_id":)" + h.next_message_id() +
                 R"(,"i":0,"kind":"fixed","a":500)",
             true);
  ack_or_die(h,
             R"({"msg_type":"graph_dist","message_id":)" + h.next_message_id() +
                 R"(,"i":1,"kind":"fixed","a":100)",
             true);
  ack_or_die(h,
             R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
                 R"(,"slot":0,"n_states":2,"entry":0)",
             true);
  ack_or_die(h,
             R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
                 R"(,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})",
             true);
  ack_or_die(h,
             R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
                 R"(,"i":1,"terminal":1,"timeout":null)",
             true);
  ack_or_die(h,
             R"({"msg_type":"graph_end","message_id":)" + h.next_message_id() +
                 R"(,"n_transitions":0,"n_output_actions":0)",
             true);

  ack_or_die(h,
             R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
                 R"(,"slot":1,"n_states":2,"entry":0)",
             true);
  // Its states are numbered from zero, like the first graph's: the host
  // authored this graph and counts its states the way it wrote them. The
  // distribution index is not -- that pool is genuinely shared.
  ack_or_die(h,
             R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
                 R"(,"i":0,"terminal":null,"timeout":{"dist":1,"target":1})",
             true);
  ack_or_die(h,
             R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
                 R"(,"i":1,"terminal":6,"timeout":null)",
             true);
  ack_or_die(h,
             R"({"msg_type":"graph_end","message_id":)" + h.next_message_id() +
                 R"(,"n_transitions":0,"n_output_actions":0)",
             true);

  r = h.close_set(4, 0, 0);
  REQUIRE(r.size() == 1);
  REQUIRE(type_of(r[0]) == "set_ok");
  CHECK(field(r[0], "n_graphs") == "2");
  CHECK(field(r[0], "n_states") == "4");
}

/// Run a whole trial and return its result_begin.
std::string run_trial(Host& h, int trial_id, int graph_index) {
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() + R"(,"trial_id":)" +
         std::to_string(trial_id) + R"(,"set_version":12,"graph_index":)" +
         std::to_string(graph_index));
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":)" +
         std::to_string(trial_id));
  const size_t before = h.sink.lines.size();
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);
  for (size_t i = before; i < h.sink.lines.size(); ++i)
    if (type_of(h.sink.lines[i]) == "result_begin") return h.sink.lines[i];
  FAIL("no result_begin");
  return "";
}

TEST_CASE("a trial selects its graph by index, and switching costs one field") {
  // The whole of docs/developer/daemon.md 3.2: every graph the session uses is already on
  // the device, so changing paradigm between two trials is a field on a message
  // that was going to be sent anyway. No upload, and nothing added to the ITI
  // of the trials where the type happened to change.
  Host h;
  greet(h);
  upload_two(h);

  const std::string first = run_trial(h, 1, 0);
  CHECK(field(first, "outcome") == std::to_string(static_cast<int>(TrialOutcome::Hit)));
  CHECK(field(first, "total_us") == "500000");

  const std::string second = run_trial(h, 2, 1);
  CHECK(field(second, "outcome") == std::to_string(static_cast<int>(TrialOutcome::Late)));
  CHECK(field(second, "total_us") == "100000");

  // And back, with no upload in between.
  const std::string third = run_trial(h, 3, 0);
  CHECK(field(third, "outcome") == std::to_string(static_cast<int>(TrialOutcome::Hit)));
  CHECK(field(third, "total_us") == "500000");
}

TEST_CASE("state indices are per graph, not into the shared pool") {
  // Graph 1's states live at pool indices 2 and 3, and the host must never see
  // that: it authored a two-state graph and numbered its states 0 and 1.
  Host h;
  greet(h);
  upload_two(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":4,"set_version":12,"graph_index":1)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":4)");

  const auto r = h.send(R"({"msg_type":"state","message_id":)" + h.next_message_id());
  REQUIRE(type_of(r[0]) == "state_report");
  CHECK(field(r[0], "current_state") == "0");  // not 2

  const size_t before = h.sink.lines.size();
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);

  // The visit stream says the same thing: it left state 0 for state 1.
  for (size_t i = before; i < h.sink.lines.size(); ++i) {
    if (type_of(h.sink.lines[i]) != "visit") continue;
    const std::string body = h.sink.lines[i].substr(0, h.sink.lines[i].size() - 1);
    JsonObject m(body.data(), body.size());
    JsonArray row;
    REQUIRE(m.array("v", &row));
    int32_t state_index = -1;
    REQUIRE(row.next_i32(&state_index));
    CHECK(state_index < 2);
    break;
  }
}

TEST_CASE("configure refuses a graph_index no slot answers to") {
  Host h;
  greet(h);
  upload_two(h);
  const auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                        R"(,"trial_id":5,"set_version":12,"graph_index":7)");
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "error");
  CHECK(field(r[0], "code") == "bad_index");
  CHECK(field(r[0], "context") == "graph_index");
}

TEST_CASE("a set that declares more graphs than arrive is refused") {
  // Declared up front so an oversize set is refused before the first graph is
  // sent -- and so a set_end that arrives early is caught rather than
  // committing a set with a hole in it.
  Host h;
  greet(h);
  h.open_set(3, 2);
  h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
             R"(,"slot":0,"n_states":2,"entry":0)",
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
             R"(,"n_transitions":0,"n_output_actions":0)",
         true);
  const auto r = h.close_set(2, 0, 0);
  CHECK(field(r[0], "code") == "bad_graph");
  CHECK(field(r[0], "context") == "n_graphs");
  CHECK_FALSE(h.device.has_set());
}

TEST_CASE("a graph that claims the wrong slot is refused") {
  // A set arrives in order, because a graph's states have to be a contiguous
  // slice of the shared pool. Stating the slot rather than implying it from
  // arrival order is what turns a dropped graph_begin into a refusal.
  Host h;
  greet(h);
  h.open_set(4, 2);
  const auto r = h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
                        R"(,"slot":1,"n_states":2,"entry":0)");
  CHECK(field(r[0], "code") == "bad_index");
  CHECK(field(r[0], "context") == "slot");
}

TEST_CASE("every state visit is reported as it happens") {
  // The record at the end of a trial is authoritative and stays so. What it is
  // not is a trace: something with a timestamp on it that arrives while the
  // trial is still running and survives a truncated path. See docs/developer/daemon.md 3.6.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":77,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":77)");

  const size_t before = h.sink.lines.size();
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);

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
         R"(,"trial_id":5,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":5)");
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);
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
         R"(,"trial_id":8,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":8)");
  const size_t before = h.sink.lines.size();
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);

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
         R"(,"trial_id":1,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":1)");
  const size_t before = h.sink.lines.size();
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);

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
  h.open_set(1);
  // A two-state loop with a short timeout, capped, so the path fills up.
  h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
             R"(,"slot":0,"n_states":3,"entry":0)",
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
  h.send(R"({"msg_type":"graph_end","message_id":)" + h.next_message_id() +
             R"(,"n_transitions":1,"n_output_actions":0)",
         true);
  auto r = h.close_set(3, 1, 0);
  REQUIRE(type_of(r[0]) == "set_ok");

  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":5,"set_version":1,"cap_ms":200)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":5)");
  const size_t before = h.sink.lines.size();
  for (uint32_t t = 0; t < 5000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);

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
         R"(,"trial_id":9,"set_version":7)");

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
           R"(,"trial_id":1,"set_version":7)");
    const auto r = h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() +
                          R"(,"trial_id":2)");
    CHECK(field(r[0], "code") == "unknown_trial");
    CHECK(h.device.state() == LinkState::Armed);
  }
  SUBCASE("start when the trial is line-triggered") {
    const auto c = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                          R"(,"trial_id":1,"set_version":7,"start":"line","start_line":3)");
    REQUIRE(type_of(c[0]) == "armed");
    const auto r = h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() +
                          R"(,"trial_id":1)");
    CHECK(field(r[0], "code") == "not_ready");
    // Armed still, not cancelled: the refusal is about this command, and the
    // line it is waiting for is still coming.
    CHECK(h.device.state() == LinkState::Armed);
  }
}

// ------------------------------------------------- starting on a line ---

namespace {

/// Arm `h` for one trial that starts on line 3, and leave it armed.
void arm_on_line(Host& h, int trial_id = 1, const char* start = "line") {
  const auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                        R"(,"trial_id":)" + std::to_string(trial_id) +
                        R"(,"set_version":7,"start":")" + start + R"(","start_line":3)");
  REQUIRE(type_of(r[0]) == "armed");
  REQUIRE(h.device.state() == LinkState::Armed);
}

constexpr LineBitmask kStartBit = 1u << 3;

/// The `started` lines the device sent of its own accord.
std::vector<std::string> unsolicited_starteds(const RecordingSink& sink) {
  std::vector<std::string> out;
  for (const auto& l : sink.lines)
    if (type_of(l) == "started" && field(l, "in_reply_to").empty()) out.push_back(l);
  return out;
}

}  // namespace

TEST_CASE("a trial armed on a line starts on that line's rising edge") {
  Host h;
  greet(h);
  upload_minimal(h);
  arm_on_line(h);

  // Low: nothing starts, and nothing is said about it.
  advance(h, 0, ms(10));
  CHECK(h.device.state() == LinkState::Armed);
  CHECK(unsolicited_starteds(h.sink).empty());

  // The edge. The entry action's line goes up in the same update -- one call,
  // so the pins and the timestamp cannot disagree.
  const OutputUpdate ops = advance(h, kStartBit, ms(20));
  CHECK(h.device.state() == LinkState::Running);
  CHECK((ops.set_high & (1u << 2)) != 0);

  const auto started = unsolicited_starteds(h.sink);
  REQUIRE(started.size() == 1);
  check_wire_valid(started[0]);
  CHECK(field(started[0], "trial_id") == "1");
  CHECK(field(started[0], "by") == "line");
  CHECK(field(started[0], "line") == "3");
  // The scan that saw the edge, not an acknowledgement of anything: this is the
  // instant the trial began.
  CHECK(field(started[0], "at_us") == std::to_string(ms(20)));
}

TEST_CASE("the line's own start is what the first state is stamped with") {
  // The same guarantee the deferred serial start buys, arrived at for free:
  // there is no foreground work between the edge and start(), because the scan
  // that sees one calls the other.
  Host h;
  greet(h);
  upload_minimal(h);
  arm_on_line(h);

  advance(h, 0, ms(10));
  advance(h, kStartBit, ms(20));
  scan_for(h, ms(600));

  const auto r = h.send(R"({"msg_type":"state","message_id":)" + h.next_message_id());
  // The result the run produced carries the entry state's own clock.
  bool saw = false;
  for (const auto& l : h.sink.lines) {
    if (type_of(l) != "visit") continue;
    if (field(l, "trial_id") != "1") continue;
    const std::string body = l.substr(0, l.size() - 1);
    JsonObject m(body.data(), body.size());
    JsonArray v;
    REQUIRE(m.array("v", &v));
    // [state_index, cause, transition_index, drawn_ms, entered_us, duration_us]
    JsonSpan span;
    JsonType type = JsonType::Missing;
    for (int i = 0; i < 4; ++i) REQUIRE(v.next(&span, &type));
    uint32_t entered = 0;
    REQUIRE(v.next_u32(&entered));
    CHECK(entered == ms(20));
    saw = true;
    break;
  }
  CHECK(saw);
  CHECK(type_of(r[0]) == "state_report");
}

TEST_CASE("a line already asserted when the trial is armed does not start it") {
  // An edge, not a level. A line nobody had lowered yet is not a start signal,
  // and treating it as one would start the trial on the scan after `configure`
  // -- which is to say, on the host's timing rather than the subject's.
  Host h;
  greet(h);
  upload_minimal(h);

  advance(h, kStartBit, ms(5));  // high before anything is armed
  arm_on_line(h);
  advance(h, kStartBit, ms(10));
  advance(h, kStartBit, ms(20));
  CHECK(h.device.state() == LinkState::Armed);
  CHECK(unsolicited_starteds(h.sink).empty());

  // Released, then asserted again: that is the edge.
  advance(h, 0, ms(30));
  advance(h, kStartBit, ms(40));
  CHECK(h.device.state() == LinkState::Running);
  REQUIRE(unsolicited_starteds(h.sink).size() == 1);
}

TEST_CASE("one edge starts one trial") {
  // The arming is spent by the edge that uses it, so the line going high again
  // mid-trial is an ordinary input and the trial after this one waits for its
  // own `configure`.
  Host h;
  greet(h);
  upload_minimal(h);
  arm_on_line(h);

  advance(h, 0, ms(10));
  advance(h, kStartBit, ms(20));
  advance(h, 0, ms(30));
  advance(h, kStartBit, ms(40));
  CHECK(unsolicited_starteds(h.sink).size() == 1);

  scan_for(h, ms(600));
  CHECK(h.device.state() == LinkState::Idle);
  // The run ended; the line is not armed for another.
  advance(h, 0, h.now + 1000);
  advance(h, kStartBit, h.now + 2000);
  CHECK(h.device.state() == LinkState::Idle);
  CHECK(unsolicited_starteds(h.sink).size() == 1);
}

TEST_CASE("start \"both\" takes whichever comes first") {
  SUBCASE("serial wins, and the line does not start a second run") {
    Host h;
    greet(h);
    upload_minimal(h);
    arm_on_line(h, 1, "both");
    const auto r = h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() +
                          R"(,"trial_id":1)");
    REQUIRE(type_of(r[0]) == "started");
    CHECK(field(r[0], "by") == "serial");
    advance(h, 0, ms(10));
    advance(h, kStartBit, ms(20));
    // One run, announced once, by the source that actually started it.
    CHECK(unsolicited_starteds(h.sink).empty());
  }
  SUBCASE("the line wins, and serial is then refused") {
    Host h;
    greet(h);
    upload_minimal(h);
    arm_on_line(h, 1, "both");
    advance(h, 0, ms(10));
    advance(h, kStartBit, ms(20));
    REQUIRE(unsolicited_starteds(h.sink).size() == 1);
    const auto r = h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() +
                          R"(,"trial_id":1)");
    CHECK(field(r[0], "code") == "not_ready");
  }
}

TEST_CASE("a start line that could never rise is refused at configure") {
  // Not left to hang armed forever. A trial waiting on an edge the board cannot
  // produce reports nothing at all, which a host cannot tell from a subject who
  // has not responded yet -- so the refusal has to be here, while there is
  // still a message to refuse.
  Host h;
  greet(h);
  upload_minimal(h);

  SUBCASE("no start_line at all") {
    const auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                          R"(,"trial_id":1,"set_version":7,"start":"line")");
    CHECK(field(r[0], "code") == "bad_json");
    CHECK(field(r[0], "context") == "start_line");
    CHECK(h.device.state() == LinkState::Idle);
  }
  SUBCASE("a line the board does not have") {
    // One past the last, expressed in the board's own count: the check is also
    // what keeps the shift that builds the bit mask inside the word.
    const auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                          R"(,"trial_id":1,"set_version":7,"start":"line","start_line":)" +
                          std::to_string(kMaxLines));
    CHECK(field(r[0], "code") == "bad_index");
    CHECK(h.device.state() == LinkState::Idle);
  }
  SUBCASE("a line the wiring has disabled") {
    // The conditioner zeroes it, so the bit cannot rise whatever the pin does.
    h.send(R"({"msg_type":"wiring","message_id":)" + h.next_message_id() + R"(,"enable":3)");
    const auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                          R"(,"trial_id":1,"set_version":7,"start":"line","start_line":3)");
    CHECK(field(r[0], "code") == "bad_index");
    CHECK(field(r[0], "context") == "start_line");
    CHECK(h.device.state() == LinkState::Idle);
  }
  SUBCASE("start_line is ignored when the trial starts on serial") {
    const auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                          R"(,"trial_id":1,"set_version":7,"start":"serial","start_line":)" +
                          std::to_string(kMaxLines));
    CHECK(type_of(r[0]) == "armed");
  }
}

TEST_CASE("a global timer outlives the trial that started it, and starts the next one") {
  // The whole point, and the thing the eight-wire loopback could not do on its
  // own: a trial is only armed while none is running, and the only thing that
  // moves an input on that harness is a *running* trial's output. A global
  // timer breaks the deadlock, because it is still going when the run that
  // started it has ended.
  //
  // The test plays the loopback itself -- output line 0 is fed back as input
  // line 4 on the following scan, which is the harness's own rule.
  Host h;
  greet(h);
  upload_with_a_timer(h);

  // Trial A: 100 ms in `wait`, then the terminal state starts the timer.
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":1,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":1)");

  LineBitmask word = 0;
  auto step = [&](Microseconds at) {
    const OutputUpdate ops = advance(h, word, at);
    // out 0 -> in 4, one scan later.
    if (ops.set_high & 1u) word |= 1u << 4;
    if (ops.set_low & 1u) word &= ~(1u << 4);
  };

  for (Microseconds t = ms(1); t <= ms(150); t += ms(1)) step(t);
  REQUIRE(h.device.state() == LinkState::Idle);  // A is over
  CHECK((word & (1u << 4)) == 0);                // and the timer has not fired

  // Arm B on the line the timer will drive. Nothing else can move it.
  const auto armed = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                            R"(,"trial_id":2,"set_version":7,"start":"line","start_line":4)");
  REQUIRE(type_of(armed[0]) == "armed");
  REQUIRE(h.device.state() == LinkState::Armed);

  // The timer was started at ~100 ms with a 200 ms delay, so it comes up around
  // 300 ms -- well after A ended and while B sits armed.
  for (Microseconds t = ms(151); t <= ms(400); t += ms(1)) {
    step(t);
    if (h.device.state() == LinkState::Running) break;
  }
  CHECK(h.device.state() == LinkState::Running);

  bool announced = false;
  for (const auto& l : h.sink.lines) {
    if (type_of(l) != "started" || !field(l, "in_reply_to").empty()) continue;
    CHECK(field(l, "trial_id") == "2");
    CHECK(field(l, "by") == "line");
    announced = true;
  }
  CHECK(announced);
}

TEST_CASE("the timers command says which timers may run") {
  Host h;
  greet(h);
  upload_with_a_timer(h);

  const auto r =
      h.send(R"({"msg_type":"timers","message_id":)" + h.next_message_id() + R"(,"enable":0)");
  REQUIRE(type_of(r[0]) == "ack");
  CHECK(field(r[0], "enable") == "0");
  CHECK(field(r[0], "n_timers") == "1");

  // Same trial as above; the timer's action runs and does nothing.
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":1,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":1)");
  for (Microseconds t = ms(1); t <= ms(400); t += ms(1)) advance(h, 0, t);

  const auto st = h.send(R"({"msg_type":"state","message_id":)" + h.next_message_id());
  CHECK(st[0].find(R"("timers_enabled":0)") != std::string::npos);
  CHECK(st[0].find(R"("timers_running":0)") != std::string::npos);
}

TEST_CASE("timers is refused while a trial is armed or running") {
  // The same rule as `wiring`, and the same reason: this changes what the scan
  // does, and a timer switched off under a running trial would move a timing
  // that trial's record could not account for.
  Host h;
  greet(h);
  upload_with_a_timer(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":1,"set_version":7)");
  const auto r =
      h.send(R"({"msg_type":"timers","message_id":)" + h.next_message_id() + R"(,"enable":0)");
  CHECK(field(r[0], "code") == "busy");
}

TEST_CASE("configure's timer mask lasts exactly one trial") {
  // A trial type selects its timers the way it selects its graph. The override
  // is reverted when the trial ends, like a distribution patch -- a mask that
  // outlived its trial would be a timer running, or not, that nobody could
  // account for afterwards.
  Host h;
  greet(h);
  upload_with_a_timer(h);
  h.send(R"({"msg_type":"timers","message_id":)" + h.next_message_id() + R"(,"enable":1)");

  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":1,"set_version":7,"timers":0)");
  auto st = h.send(R"({"msg_type":"state","message_id":)" + h.next_message_id());
  CHECK(st[0].find(R"("timers_enabled":0)") != std::string::npos);

  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":1)");
  for (Microseconds t = ms(1); t <= ms(400); t += ms(1)) advance(h, 0, t);
  REQUIRE(h.device.state() == LinkState::Idle);

  // Back to what the device was told, not left where the trial put it.
  st = h.send(R"({"msg_type":"state","message_id":)" + h.next_message_id());
  CHECK(st[0].find(R"("timers_enabled":1)") != std::string::npos);
}

TEST_CASE("a graph naming a timer that does not exist is refused at upload") {
  Host h;
  greet(h);
  auto r = h.open_set(7);
  r = h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
                 R"(,"slot":0,"n_states":1,"entry":0)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  r = h.send(R"({"msg_type":"graph_state","message_id":)" + h.next_message_id() +
                 R"(,"i":0,"terminal":1,"timeout":null)",
             true);
  REQUIRE(type_of(r[0]) == "ack");
  // Checked against the timers, not against the output lines: they are
  // different index spaces, and the wrong check would let this through to do
  // nothing at the moment it mattered.
  r = h.send(R"({"msg_type":"graph_action","message_id":)" + h.next_message_id() +
                 R"(,"on":"entry","kind":"timer_start","timer":99)",
             true);
  CHECK(type_of(r[0]) == "error");
  CHECK(field(r[0], "context") == "timer");
}

TEST_CASE("hello says where the timer lines are") {
  // Not derivable from the line count -- they are counted down from the top of
  // the word -- so a host writing a predicate against a timer has to be told.
  Host h;
  greet(h);
  const std::string body = h.sink.lines[0].substr(0, h.sink.lines[0].size() - 1);
  JsonObject m(body.data(), body.size());
  JsonObject caps;
  REQUIRE(m.object("caps", &caps));
  uint32_t v = 0;
  CHECK(caps.u32("max_timers", &v));
  CHECK(v == kMaxTimers);
  CHECK(caps.u32("first_timer_line", &v));
  CHECK(v == kFirstTimerLine);
}

TEST_CASE("a link lost disarms a trial waiting on a line") {
  // Otherwise a board left armed across a reconnect starts a trial the
  // returning host never asked for, on an id it has forgotten.
  Host h;
  greet(h);
  upload_minimal(h);
  arm_on_line(h);
  h.device.link_lost(ms(10));
  CHECK(h.device.state() == LinkState::Idle);

  advance(h, 0, ms(20));
  advance(h, kStartBit, ms(30));
  CHECK(h.device.state() == LinkState::Idle);
  CHECK(unsolicited_starteds(h.sink).empty());
}

TEST_CASE("configure against a graph the device does not hold is refused") {
  // A graph edit that did not land would otherwise leave the device confidently
  // running the old paradigm.
  Host h;
  greet(h);
  upload_minimal(h);
  const auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                        R"(,"trial_id":1,"set_version":8)");
  CHECK(field(r[0], "code") == "graph_mismatch");
  CHECK(h.device.state() == LinkState::Idle);
}

TEST_CASE("configure before any graph is refused") {
  Host h;
  greet(h);
  const auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                        R"(,"trial_id":1,"set_version":1)");
  CHECK(field(r[0], "code") == "not_ready");
}

TEST_CASE("cancel reports what actually happened") {
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":4,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":4)");
  advance(h, 0, 1000);

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
  advance(h, 0, 2000);
  const std::vector<std::string> result =
      without_visits({h.sink.lines.begin() + before, h.sink.lines.end()});
  REQUIRE(result.size() >= 3);
  CHECK(type_of(result.front()) == "result_begin");
  CHECK(field(result.front(), "outcome") ==
        std::to_string(static_cast<int>(TrialOutcome::Cancelled)));
}

TEST_CASE("a patch changes a duration for one trial and puts it back") {
  // PROTOCOL.md 3.3 has documented `patch` since M2 and nothing implemented it,
  // so a host that sent one got a silently unpatched trial -- a timing that is
  // quietly wrong, which is the failure this firmware is least willing to have.
  Host h;
  greet(h);
  upload_minimal(h);  // Wait --(500 ms)--> Hit

  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":1,"set_version":7,"patch":[{"i":0,"a":40}])");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":1)");
  size_t before = h.sink.lines.size();
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);

  std::vector<std::string> result =
      without_visits({h.sink.lines.begin() + before, h.sink.lines.end()});
  REQUIRE(type_of(result.front()) == "result_begin");
  CHECK(field(result.front(), "total_us") == "40000");

  // And the next trial draws the graph's own timing again: a patch that
  // outlived its trial would be a timing nobody could account for afterwards.
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":2,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":2)");
  before = h.sink.lines.size();
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);
  result = without_visits({h.sink.lines.begin() + before, h.sink.lines.end()});
  CHECK(field(result.front(), "total_us") == "500000");
}

TEST_CASE("a patch naming a distribution that does not exist is refused") {
  Host h;
  greet(h);
  upload_minimal(h);
  const auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                        R"(,"trial_id":1,"set_version":7,"patch":[{"i":9,"a":40}])");
  REQUIRE(r.size() == 1);
  CHECK(field(r[0], "code") == "bad_index");
  CHECK(field(r[0], "context") == "patch");
}

TEST_CASE("a malformed patch leaves every distribution as it was") {
  // Nothing is applied when any entry is unusable, so a trial cannot run with
  // half a patch on it.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":1,"set_version":7,"patch":[{"i":0,"a":40},{"i":9,"a":10}])");

  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":2,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":2)");
  const size_t before = h.sink.lines.size();
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);
  const std::vector<std::string> result =
      without_visits({h.sink.lines.begin() + before, h.sink.lines.end()});
  CHECK(field(result.front(), "total_us") == "500000");
}

TEST_CASE("a cancel that loses the race gets the real outcome back") {
  // The bridge must cope with asking to cancel and being told Hit. The
  // alternative is a record claiming a trial was cancelled when the animal had
  // already responded.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":6,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":6)");
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);
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
         R"(,"trial_id":4,"set_version":7)");
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
         R"(,"trial_id":1,"set_version":7)");
  const auto r = h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
                        R"(,"slot":0,"n_states":2,"entry":0)");
  CHECK(field(r[0], "code") == "busy");
  CHECK(h.device.set_version() == 7);  // untouched
}

TEST_CASE("a refused upload leaves the board holding no set at all") {
  // The regression M4c takes deliberately, and the reason it is written down
  // here rather than only in a plan. A single graph was double-buffered, so a
  // failed re-upload cost nothing; two sets do not fit in 32 KB, so the builder
  // fills the live one and a failure destroys it.
  //
  // What makes that acceptable is that it fails safe and loudly rather than
  // quietly: no set means configure is refused and every output sits at its
  // safe level, where the alternative would have been a board silently running
  // a paradigm somebody thought they had replaced. And it can only happen
  // between sessions -- an upload is refused while a trial is armed.
  Host h;
  greet(h);
  upload_minimal(h);
  REQUIRE(h.device.set_version() == 7);

  h.open_set(8);
  h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
         R"(,"slot":0,"n_states":2,"entry":0)");
  const auto r = h.send(R"({"msg_type":"graph_dist","message_id":)" + h.next_message_id() +
                        R"(,"i":4,"kind":"fixed","a":1)");
  CHECK(field(r[0], "code") == "bad_index");
  CHECK_FALSE(h.device.has_set());

  // And nothing can be armed until a whole set arrives.
  const auto c = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                        R"(,"trial_id":1,"set_version":7)");
  CHECK(field(c[0], "code") == "not_ready");
}

TEST_CASE("a second hello keeps the committed graph") {
  // Reconnecting the bridge must not cost a re-upload.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":1,"set_version":7)");
  REQUIRE(h.device.state() == LinkState::Armed);

  greet(h);
  CHECK(h.device.state() == LinkState::Idle);  // but the arming is gone
  CHECK(h.device.has_set());
  CHECK(h.device.set_version() == 7);
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
  CHECK(r[0].find(R"("has_set":false)") != std::string::npos);
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
  CHECK(h.device.has_set() == false);
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
         R"(,"trial_id":4,"set_version":7)");
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

  h.open_set(3);
  h.send(R"({"msg_type":"graph_begin","message_id":)" + h.next_message_id() +
             R"(,"slot":0,"n_states":2,"entry":0,"safe":5,"invert":255)",
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
             R"(,"n_transitions":0,"n_output_actions":0)",
         true);
  h.close_set(2, 0, 0);

  CHECK(h.device.has_set() == true);
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
         R"(,"trial_id":11,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":11)");
  for (uint32_t t = 0; t < 1000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);
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
         R"(,"trial_id":9,"set_version":7)");
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

TEST_CASE("a trial is stamped by the scan that starts it, not by the command that asked") {
  // The finding this exists to hold: `entered_us` used to come from the
  // timestamp the link read the `start` command at, while the entry state's
  // outputs waited for the next scan. On a board those are ~1.1 ms apart --
  // docs/operations/hardware.md, "Response latency and duration accuracy" --
  // so every trial's first state reported about a millisecond it had not spent,
  // and its entry action reached the pin about a millisecond after the
  // timestamp that said it had.
  //
  // They are one call now, so the gap cannot exist. Here it is asserted at a
  // scale no board would show: the command arrives at 1 000 us and the scan
  // comes at 900 000, and the entry state is stamped 900 000 -- along with the
  // output it raises, which the same call returns.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":11,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":11)");

  const size_t before = h.sink.lines.size();
  const OutputUpdate first = advance(h, 0, 900000);
  CHECK((first.set_high & (1u << 2)) != 0);  // the entry action, on this scan
  for (uint32_t t = 900100; t < 3000000u && h.device.state() == LinkState::Running; t += 100)
    advance(h, 0, t);

  for (size_t i = before; i < h.sink.lines.size(); ++i) {
    if (type_of(h.sink.lines[i]) != "visit") continue;
    const std::string body = h.sink.lines[i].substr(0, h.sink.lines[i].size() - 1);
    JsonObject m(body.data(), body.size());
    JsonArray row;
    REQUIRE(m.array("v", &row));
    // [state_index, cause, transition_index, drawn_ms, entered_us, duration_us]
    JsonSpan element;
    JsonType element_type = JsonType::Missing;
    std::string entered;
    for (int at = 0; row.next(&element, &element_type); ++at) {
      if (at == 4) entered.assign(element.p, element.n);
    }
    CHECK(entered == "900000");  // the scan's clock, not the command's 1 000
    return;
  }
  FAIL("no visit");
}

TEST_CASE(
    "a state_report between start and the first scan agrees with the started it follows") {
  // The same window from the host's side. It has been told the trial started;
  // asking what the device is doing must not answer "not running", and must not
  // answer with whatever state the previous trial ended in.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":12,"set_version":7)");
  const auto started = h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() +
                              R"(,"trial_id":12)");
  REQUIRE(type_of(started[0]) == "started");

  const auto r = h.send(R"({"msg_type":"state","message_id":)" + h.next_message_id());
  REQUIRE(type_of(r[0]) == "state_report");
  CHECK(r[0].find(R"("running":true)") != std::string::npos);
  CHECK(field(r[0], "current_state") == "0");
  CHECK(field(r[0], "trial_id") == "12");
}

TEST_CASE("a link lost between start and the first scan raises nothing") {
  // A trial begins on the scan, not on the `start` command, so there is a
  // window of one scan period in which the run has been promised to the host
  // and no pin has moved. A link lost inside it must not leave the entry
  // action to be applied by a later scan: there is no trial any more.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":9,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":9)");

  h.device.link_lost(1000);
  const OutputUpdate after_link_loss = advance(h, 0, 2000);
  CHECK((after_link_loss.set_high & (1u << 2)) == 0);
  CHECK_FALSE(h.device.state() == LinkState::Running);
}

TEST_CASE("a fail-safe does not end a trial, so a pending start still reaches the pins") {
  // fail_safe() drives every line to its safe level; it does not cancel
  // anything, and it never has. So a trial that is still Running when it
  // happens goes on running, and the entry action of one that has not been
  // scanned yet lands on the following scan like any other output the graph
  // owes.
  //
  // That is a change: it used to be discarded, because the entry outputs sat in
  // pending_ops_ where fail_safe() clears them, while the trial itself had
  // already started. The result was a running trial whose entry action never
  // reached a pin -- the "house light on at trial start silently did nothing"
  // failure in a second guise. The two are now one call and cannot come apart.
  //
  // Unreachable in the firmware either way: main.cpp calls fail_safe() at boot,
  // on the halt path, and on link loss -- and link loss cancels first, which is
  // the case above.
  Host h;
  greet(h);
  upload_minimal(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":10,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":10)");
  h.device.fail_safe();
  CHECK(h.device.state() == LinkState::Running);
  const OutputUpdate after_fail_safe = advance(h, 0, 3000);
  CHECK((after_fail_safe.set_high & (1u << 2)) != 0);
}

TEST_CASE("a lost link keeps the committed graph, so a reconnect costs no re-upload") {
  Host h;
  greet(h);
  upload_minimal(h);
  REQUIRE(h.device.has_set());

  h.device.link_lost(1000);
  CHECK(h.device.has_set());
  CHECK(h.device.set_version() == 7);

  // The bridge comes back. hello is what brings a fresh session seed; the graph
  // is already there.
  const auto r = h.send(R"({"msg_type":"hello","message_id":)" + h.next_message_id() +
                        R"(,"proto":1,"seed":"FEDCBA9876543210")");
  REQUIRE(type_of(r[0]) == "hello_ack");
  CHECK(field(r[0], "set_version") == "7");
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
  CHECK(r[0].find(
            R"("scan":{"hz":9871,"overruns":4,"worst_gap":2,"tx_stalls":3,"visits_dropped":0,)"
            R"("timers_enabled":4294967295,"timers_running":0})") != std::string::npos);
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
         R"(,"trial_id":4,"set_version":7)");
  const auto started =
      h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":4)");
  REQUIRE(type_of(started[0]) == "started");

  const OutputUpdate first = advance(h, 0, 1000);
  CHECK((first.set_high & (1u << 2)) != 0);

  // And only once: the next scan owes nothing.
  const OutputUpdate second = advance(h, 0, 2000);
  CHECK((second.set_high & (1u << 2)) == 0);
}

// ------------------------------------------------------------- the pin map ---
//
// docs/reference/protocol.md 3.6. The command exists so that a host can stop keeping its
// own copy of the board's pin table: which pin a line is, and which direction
// it has, are fixed when this firmware is compiled, and before `pins` there was
// no way to ask.

TEST_CASE("pins answers with the labels this build was compiled with") {
  DeviceIdentity board;
  board.board = "test_board";
  board.input_line_count = 3;
  board.output_line_count = 2;
  static const char* const kIn[] = {"D2", "D3", "D4"};
  static const char* const kOut[] = {"D10", "A0"};
  board.input_pin_labels = kIn;
  board.output_pin_labels = kOut;

  Host h(board);
  greet(h);

  const auto in =
      h.send(R"({"msg_type":"pins","message_id":)" + h.next_message_id() + R"(,"dir":"in")");
  REQUIRE(in.size() == 1);
  REQUIRE(type_of(in[0]) == "pin_map");
  CHECK(in[0].find(R"("dir":"in")") != std::string::npos);
  CHECK(in[0].find(R"("n":3)") != std::string::npos);
  CHECK(in[0].find(R"("pins":["D2","D3","D4"])") != std::string::npos);

  // The other direction is a different set of pins over a different numbering:
  // output line 0 is not input line 0, and a host that assumed otherwise would
  // be driving a valve from a lever's number.
  const auto out =
      h.send(R"({"msg_type":"pins","message_id":)" + h.next_message_id() + R"(,"dir":"out")");
  REQUIRE(out.size() == 1);
  CHECK(out[0].find(R"("dir":"out")") != std::string::npos);
  CHECK(out[0].find(R"("pins":["D10","A0"])") != std::string::npos);
}

TEST_CASE("pins names only the lines this board has") {
  // The label table may be longer than the board's line count -- the native HAL
  // carries one table for every line the config allows. What is answered is
  // what this board actually has, because that is what a host is allowed to
  // address.
  DeviceIdentity board;
  board.input_line_count = 2;
  board.output_line_count = 1;
  static const char* const kLabels[] = {"P0", "P1", "P2", "P3"};
  board.input_pin_labels = kLabels;
  board.output_pin_labels = kLabels;

  Host h(board);
  greet(h);
  const auto in =
      h.send(R"({"msg_type":"pins","message_id":)" + h.next_message_id() + R"(,"dir":"in")");
  REQUIRE(in.size() == 1);
  CHECK(in[0].find(R"("pins":["P0","P1"])") != std::string::npos);
}

TEST_CASE("a build with no pin map refuses rather than inventing one") {
  // The native build before it had labels, and every board flashed before this
  // command existed. A host that gets this keeps whatever it assumed -- and,
  // which is the entire point, knows that it assumed it.
  DeviceIdentity nameless;
  nameless.input_pin_labels = nullptr;
  nameless.output_pin_labels = nullptr;

  Host h(nameless);
  greet(h);
  const auto r =
      h.send(R"({"msg_type":"pins","message_id":)" + h.next_message_id() + R"(,"dir":"in")");
  REQUIRE(r.size() == 1);
  REQUIRE(type_of(r[0]) == "error");
  CHECK(r[0].find("no_pin_map") != std::string::npos);
}

TEST_CASE("pins requires a direction, and refuses one it does not know") {
  Host h;
  greet(h);
  const auto missing = h.send(R"({"msg_type":"pins","message_id":)" + h.next_message_id());
  REQUIRE(missing.size() == 1);
  REQUIRE(type_of(missing[0]) == "error");
  CHECK(missing[0].find("bad_json") != std::string::npos);

  const auto sideways = h.send(R"({"msg_type":"pins","message_id":)" + h.next_message_id() +
                               R"(,"dir":"sideways")");
  REQUIRE(sideways.size() == 1);
  REQUIRE(type_of(sideways[0]) == "error");
  CHECK(sideways[0].find("bad_field") != std::string::npos);
}

TEST_CASE("a host may not send pin_map at us") {
  // One namespace on the wire, and a device that answered its own reply type as
  // if it were a command would be a bug this way round too.
  Host h;
  greet(h);
  const auto r =
      h.send(R"({"msg_type":"pin_map","message_id":)" + h.next_message_id() + R"(,"dir":"in")");
  REQUIRE(r.size() == 1);
  REQUIRE(type_of(r[0]) == "error");
  CHECK(r[0].find("unknown_type") != std::string::npos);
}

// ---------------------------------------------------------------- autorun ---

TEST_CASE("a board told to drive itself runs trial after trial") {
  // No `configure`, no `start`, and one trial id after another: the device is
  // the authority here because there is nobody else to be one.
  Host h;
  greet(h);
  upload_relighting(h);

  const auto r = h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() +
                        R"(,"enabled":true,"seed":"0123456789ABCDEF")");
  REQUIRE(r.size() == 1);
  REQUIRE(type_of(r[0]) == "autorun_ok");
  CHECK(flag(r[0], "enabled"));
  CHECK(flag(r[0], "active"));
  CHECK(h.device.autorun_active());

  // Three runs of 500 ms with a 200 ms dwell between them fit inside 2.5 s.
  scan_for(h, ms(2500));
  const std::vector<int> ids = results(h.sink);
  REQUIRE(ids.size() >= 3);
  CHECK(ids[0] == 1);
  CHECK(ids[1] == 2);
  CHECK(ids[2] == 3);
  for (const auto& l : h.sink.lines) check_wire_valid(l);
}

TEST_CASE("the dwell between two self-driven runs is the one the graph declared") {
  Host h;
  greet(h);
  upload_relighting(h);
  h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() + R"(,"enabled":true)");

  // The first run ends at 500 ms. Nothing may start before the 200 ms dwell is
  // out, and the second run must be going once it is.
  scan_for(h, ms(650));
  REQUIRE(results(h.sink).size() == 1);
  CHECK(h.device.state() == LinkState::Relighting);
  scan_for(h, ms(100));
  CHECK(h.device.state() == LinkState::Running);
}

TEST_CASE("a terminal state with no dwell stops a self-driving board") {
  // Which is how a paradigm says "this outcome ends the session" -- the graph
  // decides, and it decides per outcome.
  Host h;
  greet(h);
  upload_minimal(h);  // its terminal state declares no dwell
  h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() + R"(,"enabled":true)");

  scan_for(h, ms(2000));
  CHECK(results(h.sink).size() == 1);
  CHECK_FALSE(h.device.autorun_active());
  CHECK(h.device.state() == LinkState::Idle);
}

TEST_CASE("a host cannot arm trials on a board that is arming its own") {
  // One authority at a time. Two of them would give two runs the same board and
  // one of them the wrong id.
  Host h;
  greet(h);
  upload_relighting(h);
  h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() + R"(,"enabled":true)");
  scan_for(h, ms(600));  // mid-dwell, so the refusal is not merely "busy running"
  REQUIRE(h.device.state() == LinkState::Relighting);

  const auto r = h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
                        R"(,"trial_id":9,"set_version":7)");
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "error");
  CHECK(field(r[0], "code") == "busy");
}

TEST_CASE("a host that greets takes the rig") {
  // The setting survives, so the next boot still comes up self-driving. The
  // driving stops, because somebody is there to do it -- and the run in flight
  // ends through the ordinary exit path rather than being abandoned.
  Host h;
  greet(h);
  upload_relighting(h);
  h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() + R"(,"enabled":true)");
  scan_for(h, ms(100));
  REQUIRE(h.device.state() == LinkState::Running);

  // Not greet(): a hello that lands mid-run cancels it, and a cancelled state
  // is a completed visit, which goes out unsolicited alongside the reply.
  const auto took_over =
      without_visits(h.send(R"({"msg_type":"hello","message_id":)" + h.next_message_id() +
                            R"(,"proto":1,"seed":"0123456789ABCDEF")"));
  REQUIRE(took_over.size() == 1);
  REQUIRE(type_of(took_over[0]) == "hello_ack");
  CHECK_FALSE(h.device.autorun_active());
  CHECK(h.device.autorun().enabled);
  // The cancelled run ends the way every run ends: on the next scan, with a
  // result, rather than being abandoned where it stood.
  scan_for(h, ms(1));
  CHECK(h.device.state() == LinkState::Idle);
  CHECK(results(h.sink).size() == 1);

  const auto r = h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id());
  REQUIRE(r.size() == 1);
  CHECK(flag(r[0], "enabled"));
  CHECK_FALSE(flag(r[0], "active"));

  scan_for(h, ms(2000));
  CHECK(results(h.sink).size() == 1);  // the cancelled one, and nothing after it
}

TEST_CASE("a self-driving board keeps going when the link goes away") {
  // The expected end of "upload a paradigm, then detach", not a fault. Nothing
  // is cancelled, which is why enabling autorun takes a command of its own
  // rather than being somewhere a dropped cable can arrive by accident.
  Host h;
  greet(h);
  upload_relighting(h);
  h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() + R"(,"enabled":true)");
  scan_for(h, ms(100));
  REQUIRE(h.device.state() == LinkState::Running);

  h.device.link_lost(h.now);
  CHECK(h.device.autorun_active());
  scan_for(h, ms(1500));
  CHECK(results(h.sink).size() >= 2);

  // And the handshake is still required of whoever comes back.
  const auto r = h.send(R"({"msg_type":"ping","message_id":)" + h.next_message_id());
  REQUIRE(r.size() == 1);
  CHECK(field(r[0], "code") == "not_ready");
}

TEST_CASE("a link that drops while nobody asked for autorun still fails safe") {
  Host h;
  greet(h);
  upload_relighting(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":4,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":4)");
  scan_for(h, ms(100));
  REQUIRE(h.device.state() == LinkState::Running);

  h.device.link_lost(h.now);
  CHECK(h.device.state() == LinkState::Idle);
  CHECK_FALSE(h.device.autorun_active());
}

TEST_CASE("autorun is refused against a set the board does not have") {
  Host h;
  greet(h);
  auto r = h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() +
                  R"(,"enabled":true)");
  REQUIRE(r.size() == 1);
  CHECK(field(r[0], "code") == "not_ready");

  upload_relighting(h);
  r = h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() +
             R"(,"enabled":true,"graph_index":3)");
  REQUIRE(r.size() == 1);
  CHECK(field(r[0], "code") == "bad_index");
  CHECK_FALSE(h.device.autorun_active());
}

// --------------------------------------------------------------- settings ---

namespace {

/// A store, near enough: one record, held in a buffer, shared between the
/// session that saved it and the session that comes up afterwards -- which is
/// the whole point of it and exactly what a power cycle looks like.
struct FakeStore : SettingsPort, SettingsWriter, SettingsReader {
  std::vector<uint8_t> bytes;
  size_t read_at = 0;
  bool works = true;

  bool has_storage() const override { return true; }
  bool save(const StoredSettings& s, uint32_t write_count) override {
    if (!works) return false;
    bytes.clear();
    return save_settings(s, write_count, *this);
  }
  SettingsError load(StoredSettings& s) override {
    read_at = 0;
    return load_settings(s, *this);
  }
  bool holds(const StoredSettings& s, uint32_t write_count) override {
    read_at = 0;
    return settings_already_stored(s, write_count, *this);
  }
  bool write(const void* src, size_t n) override {
    const uint8_t* b = static_cast<const uint8_t*>(src);
    bytes.insert(bytes.end(), b, b + n);
    return true;
  }
  bool read(void* dst, size_t n) override {
    if (read_at + n > bytes.size()) return false;
    for (size_t i = 0; i < n; ++i) static_cast<uint8_t*>(dst)[i] = bytes[read_at + i];
    read_at += n;
    return true;
  }
};

}  // namespace

TEST_CASE("a board comes back from a power cut running what it was told to run") {
  // The whole of standalone operation, in one test: upload, arm autorun, save,
  // lose the board, and have a fresh session come up driving trials with no
  // host, no hello and nothing plugged into it.
  FakeStore store;

  {
    Host h;
    h.device.set_settings_port(&store);
    greet(h);
    upload_relighting(h);
    h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() +
           R"(,"enabled":true,"seed":"0123456789ABCDEF","first_trial_id":100)");
    const auto r = h.send(R"({"msg_type":"save","message_id":)" + h.next_message_id());
    REQUIRE(r.size() == 1);
    REQUIRE(type_of(r[0]) == "saved");
    CHECK(flag(r[0], "has_set"));
    CHECK(flag(r[0], "autorun"));
    CHECK(field(r[0], "write_count") == "1");
  }

  // The power cycle. A different session object, with nothing but the store.
  Host after;
  after.device.set_settings_port(&store);
  REQUIRE(after.device.restore_settings(0) == SettingsError::None);
  CHECK(after.device.has_set());
  CHECK(after.device.set_version() == 7);
  CHECK(after.device.autorun_active());
  CHECK(after.device.settings_write_count() == 1);

  scan_for(after, ms(2000));
  const std::vector<int> ids = results(after.sink);
  REQUIRE(ids.size() >= 2);
  CHECK(ids[0] == 100);  // where the stored settings said to start counting
  CHECK(ids[1] == 101);

  // And nothing it emitted is answerable without a handshake: a board running
  // on its own is still a board that has not been greeted.
  const auto r = after.send(R"({"msg_type":"ping","message_id":1)");
  REQUIRE(r.size() == 1);
  CHECK(field(r[0], "code") == "not_ready");
}

TEST_CASE("the write counter counts, so flash wear is visible") {
  FakeStore store;
  Host h;
  h.device.set_settings_port(&store);
  greet(h);
  upload_relighting(h);

  for (int expected = 1; expected <= 3; ++expected) {
    // Something different each time, or the save would rightly write nothing.
    h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() +
           R"(,"enabled":true,"start_now":false,"first_trial_id":)" +
           std::to_string(expected * 10));
    const auto r = h.send(R"({"msg_type":"save","message_id":)" + h.next_message_id());
    REQUIRE(type_of(r[0]) == "saved");
    CHECK(flag(r[0], "written"));
    CHECK(field(r[0], "write_count") == std::to_string(expected));
  }
  CHECK(h.device.settings_write_count() == 3);

  // And it survives the board: the count is a property of the store, not of
  // whoever happens to be talking to it.
  Host after;
  after.device.set_settings_port(&store);
  REQUIRE(after.device.restore_settings(0) == SettingsError::None);
  CHECK(after.device.settings_write_count() == 3);
}

TEST_CASE("a save is refused mid-trial rather than stalling the scan") {
  // Erasing and programming data flash blocks for tens of milliseconds against
  // a 100 us scan. Refusing is the honest answer; doing it quietly would cost
  // the response window somebody is measuring.
  FakeStore store;
  Host h;
  h.device.set_settings_port(&store);
  greet(h);
  upload_relighting(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":4,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":4)");
  scan_for(h, ms(50));
  REQUIRE(h.device.state() == LinkState::Running);

  const auto r =
      without_visits(h.send(R"({"msg_type":"save","message_id":)" + h.next_message_id()));
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "error");
  CHECK(field(r[0], "code") == "busy");
}

TEST_CASE("a save that did not land is reported rather than assumed") {
  FakeStore store;
  store.works = false;
  Host h;
  h.device.set_settings_port(&store);
  greet(h);
  upload_relighting(h);

  const auto r = h.send(R"({"msg_type":"save","message_id":)" + h.next_message_id());
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "error");
  CHECK(field(r[0], "code") == "storage");
  // The counter did not move: nothing was written.
  CHECK(h.device.settings_write_count() == 0);
}

TEST_CASE("a board with nowhere to keep settings says so") {
  Host h;
  greet(h);
  upload_relighting(h);
  const auto r = h.send(R"({"msg_type":"save","message_id":)" + h.next_message_id());
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "error");
  CHECK(field(r[0], "code") == "not_ready");
}

TEST_CASE("a stored graph set is validated, not merely trusted") {
  // The CRC says the bytes are the ones that were written. It says nothing
  // about whether they were a graph worth running, and a set with an index out
  // of range would fault at the first trial that reached it.
  FakeStore store;
  {
    Host h;
    h.device.set_settings_port(&store);
    greet(h);
    upload_relighting(h);
    h.send(R"({"msg_type":"save","message_id":)" + h.next_message_id());
  }
  // Rot a byte in the middle and fix the CRC, so the record reads back intact
  // and only validate() can catch it: the entry state of graph 0, pointed
  // somewhere that does not exist.
  StoredSettings damaged;
  GraphSet g;
  damaged.set = &g;
  REQUIRE(store.load(damaged) == SettingsError::None);
  g.graphs[0].entry = 200;
  damaged.has_set = true;
  REQUIRE(store.save(damaged, 9));

  Host after;
  after.device.set_settings_port(&store);
  CHECK(after.device.restore_settings(0) != SettingsError::None);
  CHECK_FALSE(after.device.has_set());
  CHECK_FALSE(after.device.autorun_active());
}

TEST_CASE("a board can be told to run itself later without starting now") {
  // The sequence a rig is actually set up with. A board already arming its own
  // trials is never idle, and a save is refused on a board that is running --
  // so "enable it, then write it down" has to be performable while nothing is
  // running, or it could not be performed at all.
  FakeStore store;
  Host h;
  h.device.set_settings_port(&store);
  greet(h);
  upload_relighting(h);

  const auto r = h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() +
                        R"(,"enabled":true,"start_now":false)");
  REQUIRE(r.size() == 1);
  CHECK(flag(r[0], "enabled"));
  CHECK_FALSE(flag(r[0], "active"));
  CHECK_FALSE(h.device.autorun_active());

  // Nothing is running, so the settings can be written down.
  const auto saved = h.send(R"({"msg_type":"save","message_id":)" + h.next_message_id());
  REQUIRE(type_of(saved[0]) == "saved");
  CHECK(flag(saved[0], "autorun"));

  scan_for(h, ms(2000));
  CHECK(results(h.sink).empty());  // and it did not quietly start anyway

  // The power cycle is what turns the stored intent into a running board.
  Host after;
  after.device.set_settings_port(&store);
  REQUIRE(after.device.restore_settings(0) == SettingsError::None);
  CHECK(after.device.autorun_active());
}

TEST_CASE("stopping a self-driving board is never refused as busy") {
  // "Stop" is the thing somebody most wants while it is running, and it is the
  // one command that must not be refused for the reason that it is running. It
  // ends the run through the ordinary exit path, exactly as a `cancel` does.
  Host h;
  greet(h);
  upload_relighting(h);
  h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() + R"(,"enabled":true)");
  scan_for(h, ms(100));
  REQUIRE(h.device.state() == LinkState::Running);

  const auto r = without_visits(h.send(R"({"msg_type":"autorun","message_id":)" +
                                       h.next_message_id() + R"(,"enabled":false)"));
  REQUIRE(r.size() == 1);
  REQUIRE(type_of(r[0]) == "autorun_ok");
  CHECK_FALSE(flag(r[0], "active"));

  scan_for(h, ms(2000));
  CHECK(results(h.sink).size() == 1);  // the cancelled one, and nothing after it
}

TEST_CASE("a host cannot hand a busy board a new job") {
  Host h;
  greet(h);
  upload_relighting(h);
  h.send(R"({"msg_type":"configure","message_id":)" + h.next_message_id() +
         R"(,"trial_id":4,"set_version":7)");
  h.send(R"({"msg_type":"start","message_id":)" + h.next_message_id() + R"(,"trial_id":4)");
  scan_for(h, ms(50));
  REQUIRE(h.device.state() == LinkState::Running);

  const auto r = without_visits(h.send(R"({"msg_type":"autorun","message_id":)" +
                                       h.next_message_id() + R"(,"enabled":true)"));
  REQUIRE(r.size() == 1);
  CHECK(type_of(r[0]) == "error");
  CHECK(field(r[0], "code") == "busy");
}

TEST_CASE("state_report says who is arming the trials, and still parses") {
  // It carries kJsonMaxMembers top-level members exactly. One more would be a
  // message this device's own reader refuses, so this checks the reply is
  // parsable as well as correct -- an unparsable state_report would strand a
  // bridge that polls it.
  Host h;
  greet(h);
  upload_relighting(h);

  auto r = h.send(R"({"msg_type":"state","message_id":)" + h.next_message_id());
  REQUIRE(type_of(r[0]) == "state_report");
  CHECK_FALSE(flag(r[0], "autorun"));

  h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() + R"(,"enabled":true)");
  r = without_visits(h.send(R"({"msg_type":"state","message_id":)" + h.next_message_id()));
  REQUIRE(type_of(r[0]) == "state_report");
  CHECK(flag(r[0], "autorun"));
  // Parsable, which `field` returning a number rather than "" is the proof of.
  CHECK(field(r[0], "link_state") != "");
}

TEST_CASE("a save that would change nothing writes nothing") {
  // Data flash is good for about 100,000 erase cycles, and there is a button in
  // the web UI that invites being pressed twice. Spending one of those to store
  // the bytes that are already there is the kind of waste that presents years
  // later as a board that stops accepting settings.
  FakeStore store;
  Host h;
  h.device.set_settings_port(&store);
  greet(h);
  upload_relighting(h);

  auto r = h.send(R"({"msg_type":"save","message_id":)" + h.next_message_id());
  REQUIRE(type_of(r[0]) == "saved");
  CHECK(flag(r[0], "written"));
  CHECK(field(r[0], "write_count") == "1");
  const size_t stored_bytes = store.bytes.size();

  // Again, unchanged. Answered, not refused -- "it is already saved" is a
  // success, and a caller should not have to tell the two apart to know its
  // settings are safe.
  r = h.send(R"({"msg_type":"save","message_id":)" + h.next_message_id());
  REQUIRE(type_of(r[0]) == "saved");
  CHECK_FALSE(flag(r[0], "written"));
  CHECK(field(r[0], "write_count") == "1");  // and the counter did not move
  CHECK(store.bytes.size() == stored_bytes);
  CHECK(h.device.settings_write_count() == 1);

  // Change one thing and it writes again.
  h.send(R"({"msg_type":"autorun","message_id":)" + h.next_message_id() +
         R"(,"enabled":true,"start_now":false)");
  r = h.send(R"({"msg_type":"save","message_id":)" + h.next_message_id());
  REQUIRE(type_of(r[0]) == "saved");
  CHECK(flag(r[0], "written"));
  CHECK(field(r[0], "write_count") == "2");
}

TEST_CASE("a board whose store is blank writes, whatever the counter says") {
  // The comparison must not mistake erased flash for "already stored" -- a
  // board that never saved would then never save.
  FakeStore store;
  store.bytes.assign(64, 0xFF);
  Host h;
  h.device.set_settings_port(&store);
  greet(h);
  upload_relighting(h);

  const auto r = h.send(R"({"msg_type":"save","message_id":)" + h.next_message_id());
  REQUIRE(type_of(r[0]) == "saved");
  CHECK(flag(r[0], "written"));
}
