// SPDX-License-Identifier: GPL-3.0-or-later
// The graph upload. Everything a host can get wrong about it arrives here, and
// a graph that is accepted is one the rig will run for the next three hundred
// trials -- so these tests are mostly about refusal, and about refusing with a
// message that names what to change.
#include <deque>
#include <string>

#include "doctest.h"
#include "machine/state_machine.h"
#include "protocol/crc16.h"
#include "protocol/framing.h"
#include "protocol/graph_builder.h"
#include "trial/trial.h"

using namespace statemachined;

namespace {

/// Drives the builder the way the session will: frame the line, verify it,
/// parse it, dispatch on `t`. Going through the real framing and JSON layers
/// rather than calling the builder directly is the point -- the checksum is
/// over the CRC-covered bytes, and a test that made those up would not be
/// testing the thing the host has to reproduce.
struct Upload {
  GraphBuilder builder;
  std::deque<std::string> keep;  ///< spans borrow; the text must outlive them
  uint16_t checksum = 0xFFFF;    ///< what the host would have accumulated

  /// `body` is the object without its closing brace, as the writer produces it.
  UploadError send(const std::string& body, bool fold = true) {
    char buf[kMaxLine];
    REQUIRE(body.size() < sizeof(buf));
    for (size_t i = 0; i < body.size(); ++i) buf[i] = body[i];
    const size_t n = finish_frame(buf, body.size(), sizeof(buf), false);
    REQUIRE(n > 0);

    keep.emplace_back(buf, n);
    const std::string& line = keep.back();
    Frame f;
    REQUIRE(verify_frame(line.data(), line.size(), &f) == FrameError::None);
    const JsonSpan covered{f.covered, f.covered_len};
    if (fold) checksum = crc16_ccitt(covered.p, covered.n, checksum);

    JsonObject m(line.data(), line.size());
    REQUIRE(m.valid());
    JsonSpan t;
    REQUIRE(m.str("msg_type", &t));

    if (json_str_eq(t, "graph_begin")) return builder.begin(m, covered);
    if (json_str_eq(t, "graph_dist")) return builder.add_distribution(m, covered);
    if (json_str_eq(t, "graph_state")) return builder.add_state(m, covered);
    if (json_str_eq(t, "graph_transition")) return builder.add_transition(m, covered);
    if (json_str_eq(t, "graph_action")) return builder.add_action(m, covered);
    if (json_str_eq(t, "graph_end")) return builder.end(m);
    FAIL("unknown message type in test");
    return UploadError::BadField;
  }

  std::string checksum_hex() const {
    char h[4];
    crc16_to_hex(checksum, h);
    return std::string(h, 4);
  }

  UploadError finish(int n_transitions, int n_actions) {
    return send(R"({"msg_type":"graph_end","message_id":9,"n_transitions":)" +
                std::to_string(n_transitions) + R"(,"n_output_actions":)" +
                std::to_string(n_actions) + R"(,"checksum":")" + checksum_hex() + R"(")");
  }
};

/// Two states: wait, with a 500 ms timeout, going to a terminal Hit.
void minimal(Upload& u) {
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":7,"n_states":2,"entry":0)") ==
      UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"fixed","a":500)") ==
          UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})") ==
      UploadError::None);
  REQUIRE(
      u.send(R"({"msg_type":"graph_state","message_id":4,"i":1,"terminal":1,"timeout":null)") ==
      UploadError::None);
}

}  // namespace

TEST_CASE("a well-formed upload assembles, validates and commits") {
  Upload u;
  minimal(u);
  REQUIRE(u.finish(0, 0) == UploadError::None);
  REQUIRE(u.builder.complete());

  const StateGraph& g = u.builder.staged();
  CHECK(g.version == 7);
  CHECK(g.n_states == 2);
  CHECK(g.entry == 0);
  CHECK(g.n_distributions == 1);
  CHECK(g.states[0].timeout_duration == 0);
  CHECK(g.states[0].timeout_target == 1);
  CHECK_FALSE(g.states[0].terminal());
  CHECK(g.states[1].terminal());
  CHECK(g.states[1].terminal_code == terminal_code_of(TrialOutcome::Hit));
  CHECK(validate(g) == GraphError::None);
}

TEST_CASE("the assembled graph actually runs") {
  // The point of the upload is a graph the machine can execute. Asserting on
  // the struct alone would let a subtly wrong slice through.
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":7,"n_states":2,"entry":0)") ==
      UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"fixed","a":500)") ==
          UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})") ==
      UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_action","message_id":4,"on":"entry","line":2,"kind":"high")") ==
      UploadError::None);
  REQUIRE(
      u.send(R"({"msg_type":"graph_state","message_id":5,"i":1,"terminal":1,"timeout":null)") ==
      UploadError::None);
  REQUIRE(u.finish(0, 1) == UploadError::None);

  StateMachine m(u.builder.staged());
  const OutputUpdate on_entry = m.start(1, 0);
  CHECK((on_entry.set_high & (1u << 2)) != 0);

  uint32_t t = 0;
  LineBitmask lowered = 0;
  while (m.is_running() && t < 2000000u) {
    t += 100;
    lowered |= m.advance(0, t).set_low;
  }
  CHECK_FALSE(m.is_running());
  CHECK(m.get_record().terminal_code == terminal_code_of(TrialOutcome::Hit));
  CHECK((lowered & (1u << 2)) != 0);  // the line the entry action raised came down
}

TEST_CASE("a terminal state's entry actions run -- that is how a reward is written") {
  // "Pulse the valve on entering Hit" is the natural way to say it. Hanging it
  // off the exit of whichever state happened to precede the terminal one would
  // spread a single intention over every route into it.
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
      UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"fixed","a":10)") ==
          UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})") ==
      UploadError::None);
  REQUIRE(
      u.send(R"({"msg_type":"graph_state","message_id":4,"i":1,"terminal":1,"timeout":null)") ==
      UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_action","message_id":5,"on":"entry","line":3,"kind":"pulse","ms":40)") ==
      UploadError::None);
  REQUIRE(u.finish(0, 1) == UploadError::None);

  StateMachine m(u.builder.staged());
  LineBitmask raised = m.start(1, 0).set_high;
  uint32_t t = 0;
  while (m.is_running() && t < 1000000u) {
    t += 100;
    raised |= m.advance(0, t).set_high;
  }
  CHECK_FALSE(m.is_running());
  CHECK((raised & (1u << 3)) != 0);  // the valve opened
  CHECK(m.get_record().terminal_code == terminal_code_of(TrialOutcome::Hit));
}

TEST_CASE("what a terminal state raised is lowered when the next run starts") {
  // Nothing exits a terminal state, so the machine cannot lower those lines
  // during the run. They are carried instead, and start() pays the debt -- a
  // line left high by the last trial cannot survive into the next one
  // unnoticed, which is the guarantee leave() already makes within a run.
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
      UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"fixed","a":10)") ==
          UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})") ==
      UploadError::None);
  REQUIRE(
      u.send(R"({"msg_type":"graph_state","message_id":4,"i":1,"terminal":1,"timeout":null)") ==
      UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_action","message_id":5,"on":"entry","line":6,"kind":"high")") ==
      UploadError::None);
  REQUIRE(u.finish(0, 1) == UploadError::None);

  StateMachine m(u.builder.staged());
  m.start(1, 0);
  uint32_t t = 0;
  LineBitmask raised = 0;
  while (m.is_running() && t < 1000000u) {
    t += 100;
    raised |= m.advance(0, t).set_high;
  }
  REQUIRE((raised & (1u << 6)) != 0);  // still high after the run ended

  const OutputUpdate next = m.start(2, t + 1000);
  CHECK((next.set_low & (1u << 6)) != 0);
}

TEST_CASE("transitions and actions attach to the state that preceded them") {
  // The ordering rule on the wire is the memory invariant: a state owns its
  // transitions as a (first, count) slice of one flat pool, and a slice is
  // contiguous only if everything for a state arrives together.
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":3,"entry":0)") ==
      UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"fixed","a":100)") ==
          UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":null,"timeout":{"dist":0,"target":2})") ==
      UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_transition","message_id":4,"all":1,"target":1)") ==
          UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_transition","message_id":5,"all":2,"target":2)") ==
          UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_state","message_id":6,"i":1,"terminal":null,"timeout":{"dist":0,"target":2})") ==
      UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_transition","message_id":7,"all":4,"target":2)") ==
          UploadError::None);
  REQUIRE(
      u.send(R"({"msg_type":"graph_state","message_id":8,"i":2,"terminal":1,"timeout":null)") ==
      UploadError::None);
  REQUIRE(u.finish(3, 0) == UploadError::None);

  const StateGraph& g = u.builder.staged();
  CHECK(g.states[0].first_transition == 0);
  CHECK(g.states[0].transition_count == 2);
  CHECK(g.states[1].first_transition == 2);
  CHECK(g.states[1].transition_count == 1);
  CHECK(g.states[2].transition_count == 0);
  CHECK(g.transitions[0].all_high == 1);
  CHECK(g.transitions[2].all_high == 4);
}

TEST_CASE("entry and exit actions are two contiguous slices of one pool") {
  Upload u;
  minimal(u);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_action","message_id":5,"on":"entry","line":0,"kind":"high")") ==
      UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_action","message_id":6,"on":"entry","line":1,"kind":"pulse","ms":50)") ==
      UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_action","message_id":7,"on":"exit","line":2,"kind":"low")") ==
      UploadError::None);
  REQUIRE(u.finish(0, 3) == UploadError::None);

  const StateGraph& g = u.builder.staged();
  const State& s = g.states[1];
  CHECK(s.first_entry_action == 0);
  CHECK(s.entry_action_count == 2);
  CHECK(s.first_exit_action == 2);
  CHECK(s.exit_action_count == 1);
  CHECK(g.output_actions[1].kind == OutputActionKind::Pulse);
  CHECK(g.output_actions[1].pulse_ms == 50);
  CHECK(g.output_actions[2].kind == OutputActionKind::Low);
}

TEST_CASE("an entry action after an exit action is refused") {
  // Interleaving them would silently give one slice the other's members: the
  // two are slices of the same pool and each has to be contiguous.
  Upload u;
  minimal(u);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_action","message_id":5,"on":"exit","line":0,"kind":"low")") ==
      UploadError::None);
  CHECK(
      u.send(
          R"({"msg_type":"graph_action","message_id":6,"on":"entry","line":1,"kind":"high")") ==
      UploadError::BadOrder);
  CHECK(std::string(u.builder.context()).find("entry action") != std::string::npos);
}

TEST_CASE("a dropped message is caught by the checksum, not by luck") {
  // Every line carries its own crc, which catches a corrupt message. This
  // catches a missing one -- which no per-line check can see, because the line
  // that vanished was perfectly well formed.
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":7,"n_states":2,"entry":0)") ==
      UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"fixed","a":500)") ==
          UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})") ==
      UploadError::None);
  REQUIRE(
      u.send(R"({"msg_type":"graph_state","message_id":4,"i":1,"terminal":1,"timeout":null)") ==
      UploadError::None);

  // The host thinks it also sent something the device never saw.
  u.checksum = crc16_ccitt("a line that never arrived", 25, u.checksum);
  CHECK(u.finish(0, 0) == UploadError::ChecksumMismatch);
  CHECK_FALSE(u.builder.complete());
}

TEST_CASE("the totals in graph_end are checked against what arrived") {
  Upload u;
  minimal(u);
  CHECK(u.finish(1, 0) == UploadError::CountMismatch);
  CHECK(std::string(u.builder.context()) == "n_transitions");
}

TEST_CASE("fewer states than graph_begin declared is refused") {
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":7,"n_states":3,"entry":0)") ==
      UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"fixed","a":500)") ==
          UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})") ==
      UploadError::None);
  REQUIRE(
      u.send(R"({"msg_type":"graph_state","message_id":4,"i":1,"terminal":1,"timeout":null)") ==
      UploadError::None);
  CHECK(u.finish(0, 0) == UploadError::CountMismatch);
  CHECK(std::string(u.builder.context()) == "n_states");
}

TEST_CASE("an index that does not follow the ones accepted is refused") {
  // Stated rather than implied by arrival order, so a dropped message becomes a
  // refusal instead of a silently mis-indexed graph.
  SUBCASE("a state") {
    Upload u;
    REQUIRE(
        u.send(
            R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
        UploadError::None);
    CHECK(
        u.send(
            R"({"msg_type":"graph_state","message_id":2,"i":1,"terminal":1,"timeout":null)") ==
        UploadError::BadIndex);
  }
  SUBCASE("a distribution") {
    Upload u;
    REQUIRE(
        u.send(
            R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
        UploadError::None);
    CHECK(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":3,"kind":"fixed","a":1)") ==
          UploadError::BadIndex);
  }
  SUBCASE("more states than declared") {
    Upload u;
    minimal(u);
    CHECK(
        u.send(
            R"({"msg_type":"graph_state","message_id":9,"i":2,"terminal":1,"timeout":null)") ==
        UploadError::BadIndex);
  }
}

TEST_CASE("a graph message out of order is refused") {
  SUBCASE("anything before graph_begin") {
    Upload u;
    CHECK(
        u.send(
            R"({"msg_type":"graph_state","message_id":1,"i":0,"terminal":1,"timeout":null)") ==
        UploadError::NotOpen);
    Upload v;
    CHECK(
        v.send(
            R"({"msg_type":"graph_end","message_id":1,"n_transitions":0,"n_output_actions":0,"checksum":"0000")") ==
        UploadError::NotOpen);
  }
  SUBCASE("a distribution after the first state") {
    // States are range-checked against the distribution pool as they arrive, so
    // the pool has to be complete first.
    Upload u;
    minimal(u);
    CHECK(u.send(R"({"msg_type":"graph_dist","message_id":9,"i":1,"kind":"fixed","a":1)") ==
          UploadError::BadOrder);
  }
  SUBCASE("a transition with no state to attach to") {
    Upload u;
    REQUIRE(
        u.send(
            R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
        UploadError::None);
    CHECK(u.send(R"({"msg_type":"graph_transition","message_id":2,"all":1,"target":1)") ==
          UploadError::BadOrder);
  }
  SUBCASE("an action with no state to attach to") {
    Upload u;
    REQUIRE(
        u.send(
            R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
        UploadError::None);
    CHECK(
        u.send(
            R"({"msg_type":"graph_action","message_id":2,"on":"entry","line":0,"kind":"high")") ==
        UploadError::BadOrder);
  }
}

TEST_CASE("a capacity is refused by name, so the answer is which limit to raise") {
  Upload u;
  const std::string begin =
      R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":)" +
      std::to_string(kMaxStates + 1) + R"(,"entry":0)";
  CHECK(u.send(begin) == UploadError::TooMany);
  CHECK(std::string(u.builder.context()) == "max_states");

  Upload v;
  REQUIRE(
      v.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
      UploadError::None);
  UploadError last = UploadError::None;
  for (int i = 0; i <= kMaxDistributions && last == UploadError::None; ++i)
    last = v.send(R"({"msg_type":"graph_dist","message_id":2,"i":)" + std::to_string(i) +
                  R"(,"kind":"fixed","a":1)");
  CHECK(last == UploadError::TooMany);
  CHECK(std::string(v.builder.context()) == "max_distributions");
}

TEST_CASE("a transition with no predicate at all is refused") {
  // It would hold on the first evaluation of every state it is in, which is
  // never what an experimenter meant to write.
  Upload u;
  minimal(u);
  CHECK(u.send(R"({"msg_type":"graph_transition","message_id":9,"target":1)") ==
        UploadError::BadField);
  CHECK(std::string(u.builder.context()).find("predicate") != std::string::npos);
}

TEST_CASE("a choice distribution keeps its options in the graph's own pool") {
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
      UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"choice","opts":[100,200,300])") ==
      UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_dist","message_id":3,"i":1,"kind":"choice","opts":[7,8],"weights":[1,9])") ==
      UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_state","message_id":4,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})") ==
      UploadError::None);
  REQUIRE(
      u.send(R"({"msg_type":"graph_state","message_id":5,"i":1,"terminal":1,"timeout":null)") ==
      UploadError::None);
  REQUIRE(u.finish(0, 0) == UploadError::None);

  const StateGraph& g = u.builder.staged();
  REQUIRE(g.n_choice_options == 5);
  CHECK(g.distributions[0].n == 3);
  CHECK(g.distributions[0].opts == &g.choice_options[0]);
  CHECK(g.distributions[0].weights == nullptr);  // unweighted stays unweighted
  CHECK(g.distributions[1].n == 2);
  CHECK(g.distributions[1].opts == &g.choice_options[3]);
  CHECK(g.distributions[1].weights == &g.choice_weights[3]);
  CHECK(g.choice_weights[3] == 1);
  CHECK(g.choice_weights[4] == 9);

  // And a draw only ever yields a listed option.
  Rng r(42);
  for (int i = 0; i < 200; ++i) {
    const Milliseconds v = g.distributions[0].draw(r);
    CHECK((v == 100 || v == 200 || v == 300));
  }
}

TEST_CASE("committing a graph by copy re-points its choice options") {
  // The session holds the staged graph and the live one separately, so
  // committing is a copy. A memberwise copy would leave the new graph's
  // distributions pointing into the old graph's arrays -- which works, silently,
  // until the next upload overwrites them and a foreperiod is drawn from
  // whatever a later paradigm left behind.
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
      UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"choice","opts":[100,200])") ==
      UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":null,"timeout":{"dist":0,"target":1})") ==
      UploadError::None);
  REQUIRE(
      u.send(R"({"msg_type":"graph_state","message_id":4,"i":1,"terminal":1,"timeout":null)") ==
      UploadError::None);
  REQUIRE(u.finish(0, 0) == UploadError::None);

  StateGraph live = u.builder.staged();
  CHECK(live.distributions[0].opts == &live.choice_options[0]);
  CHECK(live.distributions[0].opts != u.builder.staged().distributions[0].opts);
  CHECK(live.choice_options[0] == 100);
  CHECK(live.choice_options[1] == 200);
  CHECK(validate(live) == GraphError::None);

  Rng r(1);
  for (int i = 0; i < 100; ++i) {
    const Milliseconds v = live.distributions[0].draw(r);
    CHECK((v == 100 || v == 200));
  }
}

TEST_CASE("a distribution pointing at a static array survives a copy untouched") {
  // A hand-built graph -- which is how every other test in this repo makes one
  // -- never pointed into a pool, so the copy must leave it exactly as it was.
  static const Milliseconds kOpts[] = {5, 6, 7};
  StateGraph a;
  a.n_distributions = 1;
  a.distributions[0].kind = RandomDistributionKind::Choice;
  a.distributions[0].n = 3;
  a.distributions[0].opts = kOpts;

  const StateGraph b = a;
  CHECK(b.distributions[0].opts == kOpts);
}

TEST_CASE("weights must line up with the options they weight") {
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
      UploadError::None);
  SUBCASE("shorter") {
    // Silently giving the unlisted options weight 1 against neighbours weighted
    // in the hundreds would bias a paradigm in a way nobody would notice.
    CHECK(
        u.send(
            R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"choice","opts":[1,2,3],"weights":[5,5])") ==
        UploadError::BadField);
  }
  SUBCASE("longer") {
    CHECK(
        u.send(
            R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"choice","opts":[1],"weights":[5,5])") ==
        UploadError::BadField);
  }
  SUBCASE("empty options") {
    CHECK(
        u.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"choice","opts":[])") ==
        UploadError::BadField);
  }
}

TEST_CASE("a malformed or missing member is refused by name") {
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
      UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"fixed","a":100)") ==
          UploadError::None);

  SUBCASE("terminal is not optional -- absent is different from null") {
    // A graph_state that forgot to say is a bug in the sender, and is different
    // from one that said "not terminal".
    CHECK(u.send(R"({"msg_type":"graph_state","message_id":3,"i":0,"timeout":null)") ==
          UploadError::BadField);
    CHECK(std::string(u.builder.context()) == "terminal");
  }
  SUBCASE("an unknown distribution kind") {
    Upload v;
    REQUIRE(
        v.send(
            R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
        UploadError::None);
    CHECK(v.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"gaussian","a":1)") ==
          UploadError::BadField);
    CHECK(std::string(v.builder.context()) == "kind");
  }
  SUBCASE("an unknown action kind") {
    REQUIRE(
        u.send(
            R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":1,"timeout":null)") ==
        UploadError::None);
    CHECK(
        u.send(
            R"({"msg_type":"graph_action","message_id":4,"on":"entry","line":0,"kind":"wiggle")") ==
        UploadError::BadField);
  }
  SUBCASE("a pulse with no duration") {
    REQUIRE(
        u.send(
            R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":1,"timeout":null)") ==
        UploadError::None);
    CHECK(
        u.send(
            R"({"msg_type":"graph_action","message_id":4,"on":"entry","line":0,"kind":"pulse")") ==
        UploadError::BadField);
  }
  SUBCASE("an output line the board does not have") {
    REQUIRE(
        u.send(
            R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":1,"timeout":null)") ==
        UploadError::None);
    CHECK(u.send(R"({"msg_type":"graph_action","message_id":4,"on":"entry","line":)" +
                 std::to_string(kMaxOutputLines) + R"(,"kind":"high")") ==
          UploadError::BadField);
  }
  SUBCASE("a timeout naming a distribution that does not exist") {
    CHECK(
        u.send(
            R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":null,"timeout":{"dist":5,"target":1})") ==
        UploadError::BadIndex);
    CHECK(std::string(u.builder.context()) == "timeout.dist");
  }
  SUBCASE("an entry state that does not exist") {
    Upload v;
    CHECK(
        v.send(
            R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":5)") ==
        UploadError::BadField);
    CHECK(std::string(v.builder.context()) == "entry");
  }
}

TEST_CASE("a graph that assembles but cannot run is refused, naming the rule") {
  // Validation is the last gate, and its verdict is passed through rather than
  // flattened into "bad graph".
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
      UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"fixed","a":100)") ==
          UploadError::None);
  // Both states non-terminal, and the second unreachable.
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":null,"timeout":{"dist":0,"target":0})") ==
      UploadError::None);
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_state","message_id":4,"i":1,"terminal":null,"timeout":{"dist":0,"target":1})") ==
      UploadError::None);
  CHECK(u.finish(0, 0) == UploadError::Invalid);
  CHECK(u.builder.graph_error() == GraphError::NoTerminal);
  CHECK(std::string(u.builder.context()) ==
        std::string(graph_error_str(GraphError::NoTerminal)));
}

TEST_CASE("a refused message abandons the upload rather than half-building one") {
  // The alternative is a graph assembled from two different attempts.
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
      UploadError::None);
  REQUIRE(u.builder.open());
  CHECK(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":9,"kind":"fixed","a":1)") ==
        UploadError::BadIndex);
  CHECK_FALSE(u.builder.open());
  CHECK(
      u.send(R"({"msg_type":"graph_state","message_id":3,"i":0,"terminal":1,"timeout":null)") ==
      UploadError::NotOpen);
}

TEST_CASE("a second graph_begin discards the first attempt") {
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
      UploadError::None);
  REQUIRE(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"fixed","a":1)") ==
          UploadError::None);

  Upload v;  // a fresh accumulator; the device resets its own
  v.builder = u.builder;
  v.checksum = 0xFFFF;
  REQUIRE(
      v.send(
          R"({"msg_type":"graph_begin","message_id":3,"graph_version":2,"n_states":2,"entry":0)") ==
      UploadError::None);
  CHECK(v.builder.staged().n_distributions == 0);
  CHECK(v.builder.staged().version == 2);
}

TEST_CASE("abandon throws away a partial upload") {
  Upload u;
  REQUIRE(
      u.send(
          R"({"msg_type":"graph_begin","message_id":1,"graph_version":1,"n_states":2,"entry":0)") ==
      UploadError::None);
  u.builder.abandon();
  CHECK_FALSE(u.builder.open());
  CHECK(u.send(R"({"msg_type":"graph_dist","message_id":2,"i":0,"kind":"fixed","a":1)") ==
        UploadError::NotOpen);
}

TEST_CASE("every upload error has a message") {
  const UploadError every[] = {
      UploadError::None,          UploadError::NotOpen,          UploadError::BadOrder,
      UploadError::BadIndex,      UploadError::TooMany,          UploadError::BadField,
      UploadError::CountMismatch, UploadError::ChecksumMismatch, UploadError::Invalid};
  for (UploadError e : every) {
    const char* m = upload_error_str(e);
    REQUIRE(m != nullptr);
    CHECK(m[0] != '\0');
  }
}
