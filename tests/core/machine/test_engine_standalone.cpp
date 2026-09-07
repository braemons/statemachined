// SPDX-License-Identifier: GPL-3.0-or-later
// The engine with no wire anywhere near it.
//
// This binary links against statemachined_engine alone -- no framing, no JSON, no
// session object is on its command line. So it is a *link-time* proof, not a
// claim in a comment: if the state machine ever grows a dependency on the
// protocol, this fails to link.
//
// It matters beyond tidiness. A graph built in C++ and run directly is what a
// bench rig, a simulator, or a Linux gpiochip embedding would do, and it is
// what makes the native build a real test of the firmware rather than a
// parallel implementation of it. The protocol is one way to get a graph in, not
// the only way.
#include "doctest.h"
#include "helpers.h"
#include "trial/trial_runner.h"

using namespace statemachined;
using namespace statemachined::test;

TEST_CASE("a graph built in C++ runs with no protocol layer linked") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.timeout(wait, b.fixed(250), hit);
  b.on_entry(wait, OutputAction{5, OutputActionKind::High, 0});
  b.entry(wait);
  REQUIRE(validate(b.g) == GraphError::None);

  TrialRunner r(b.g);
  const OutputUpdate on_entry = r.start(1, 0xABCDu, 0);
  CHECK((on_entry.set_high & bit(5)) != 0);

  uint32_t t = 0;
  LineBitmask lowered = 0;
  while (r.running() && t < ms(1000)) {
    t += 100;
    lowered |= r.advance(0, t).set_low;
  }
  CHECK_FALSE(r.running());
  CHECK(r.result().outcome == TrialOutcome::Hit);
  CHECK((lowered & bit(5)) != 0);
  CHECK(r.run_record().path_len == 2);
}

TEST_CASE("input lines drive it just as well without a host") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  const uint8_t late = b.terminal(TrialOutcome::Late);
  b.timeout(wait, b.fixed(500), late);
  Transition t;
  t.all_high = bit(0);
  t.target_state = hit;
  b.on(wait, t);
  b.entry(wait);
  REQUIRE(validate(b.g) == GraphError::None);

  TrialRunner r(b.g);
  r.start(2, 0xABCDu, 0);
  uint32_t now = 0;
  for (int i = 0; i < 100 && r.running(); ++i) {
    now += 100;
    r.advance(bit(0), now);
  }
  CHECK_FALSE(r.running());
  CHECK(r.result().outcome == TrialOutcome::Hit);
}

TEST_CASE("the run is reproducible from the seed, with nothing else involved") {
  // The reproducibility claim belongs to the engine, not to the link: the same
  // seed gives the same realised durations whether a host is attached or not.
  const auto draw = [](uint64_t seed) {
    Builder b;
    const uint8_t wait = b.state();
    const uint8_t hit = b.terminal(TrialOutcome::Hit);
    b.timeout(wait, b.uniform(100, 900), hit);
    b.entry(wait);
    TrialRunner r(b.g);
    r.start(7, seed, 0);
    // A visit is recorded when its state is left, not when it is entered, so
    // the run has to finish before the realised duration is there to read.
    uint32_t t = 0;
    while (r.running() && t < ms(5000)) {
      t += 100;
      r.advance(0, t);
    }
    REQUIRE(r.run_record().path_len > 0);
    return r.run_record().path[0].drawn_ms;
  };
  CHECK(draw(0x1234u) == draw(0x1234u));
  CHECK(draw(0x1234u) != draw(0x9999u));
}
