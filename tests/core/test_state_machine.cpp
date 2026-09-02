// The state machine on its own, with no trial vocabulary anywhere in the file.
//
// That is the point of these tests as much as what they assert: if the machine
// ever grows a dependency on trial ids, outcomes or cancel reasons, this file
// stops compiling. It includes state_machine.h and never trial.h.
#include "doctest.h"
#include "helpers.h"
#include "state_machine.h"

using namespace fsmd;
using namespace fsmd::test;

namespace {
void advance(StateMachine& m, LineBitmask word, uint32_t& t, uint32_t until_us) {
  while (t < until_us) {
    t += 100;
    m.scan(word, t);
  }
}
}  // namespace

TEST_CASE("a run reports the terminal code it reached, not an interpretation") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t done = b.terminal_code(7);
  b.timeout(wait, b.fixed(10), done);
  b.g.entry = wait;
  REQUIRE(validate(b.g) == GraphError::None);

  StateMachine m;
  m.set_graph(&b.g);
  uint32_t t = 0;
  m.start(12345, t);
  CHECK(m.running());
  advance(m, 0, t, ms(20));

  CHECK_FALSE(m.running());
  CHECK(m.record().terminal_code == 7);
  CHECK_FALSE(m.record().halted);
}

TEST_CASE("halt ends a run through the ordinary exit path") {
  Builder b;
  const uint8_t hold = b.state();
  const uint8_t done = b.terminal_code(1);
  b.timeout(hold, b.fixed(10000), done);
  b.on_entry(hold, OutputAction{3, OutputActionKind::High, 0});
  b.g.entry = hold;
  REQUIRE(validate(b.g) == GraphError::None);

  StateMachine m;
  m.set_graph(&b.g);
  uint32_t t = 0;
  const OutputUpdate up = m.start(1, t);
  CHECK(up.set_high == bit(3));

  t += ms(5);
  CHECK(m.halt(t));
  CHECK_FALSE(m.running());
  CHECK(m.record().halted);
  CHECK(m.record().terminal_code == kNotTerminal);

  // Everything the state raised comes down, even though the graph declared no
  // exit action at all.
  const OutputUpdate after = m.scan(0, t + 100);
  CHECK((after.set_low & bit(3)) == bit(3));

  CHECK_FALSE(m.halt(t + 200));  // the first decision wins
}

TEST_CASE("the run cap stops a graph that validates but never ends") {
  Builder b;
  const uint8_t spin = b.state();
  const uint8_t done = b.terminal_code(1);
  // Reachable on paper, never reached in practice: nothing drives the input.
  b.on(spin, Transition{bit(0), 0, 0, done, kNoRandomDistribution, false});
  b.g.entry = spin;
  REQUIRE(validate(b.g) == GraphError::None);

  StateMachine m;
  m.set_graph(&b.g);
  m.set_run_cap_ms(50);
  uint32_t t = 0;
  m.start(1, t);
  advance(m, 0, t, ms(80));

  CHECK_FALSE(m.running());
  CHECK(m.record().hit_run_cap);
  CHECK(m.record().terminal_code == kNotTerminal);
}

TEST_CASE("the same seed gives the same run") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t done = b.terminal_code(1);
  b.timeout(wait, b.uniform(10, 200), done);
  b.g.entry = wait;

  auto run_once = [&](uint64_t seed) {
    StateMachine m;
    m.set_graph(&b.g);
    uint32_t t = 0;
    m.start(seed, t);
    advance(m, 0, t, ms(500));
    return m.record().path[0].drawn_ms;
  };

  CHECK(run_once(99) == run_once(99));
}
