// SPDX-License-Identifier: GPL-3.0-or-later
// The state machine on its own, with no trial vocabulary anywhere in the file.
//
// That is the point of these tests as much as what they assert: if the machine
// ever grows a dependency on trial ids, outcomes or cancel reasons, this file
// stops compiling. It includes state_machine.h and never trial.h.
#include "doctest.h"
#include "helpers.h"
#include "machine/state_machine.h"

using namespace statemachined;
using namespace statemachined::test;

namespace {
void run_until(StateMachine& m, LineBitmask word, uint32_t& t, uint32_t until_us) {
  while (t < until_us) {
    t += 100;
    m.advance(word, t);
  }
}
}  // namespace

TEST_CASE("a run reports the terminal code it reached, not an interpretation") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t done = b.terminal_code(7);
  b.timeout(wait, b.fixed(10), done);
  b.entry(wait);
  REQUIRE(validate(b.g) == GraphError::None);

  StateMachine m(b.g);
  uint32_t t = 0;
  m.start(12345, t);
  CHECK(m.is_running());
  run_until(m, 0, t, ms(20));

  CHECK_FALSE(m.is_running());
  CHECK(m.get_record().terminal_code == 7);
  CHECK_FALSE(m.get_record().force_ended);
}

TEST_CASE("force_end ends a run through the ordinary exit path") {
  Builder b;
  const uint8_t hold = b.state();
  const uint8_t done = b.terminal_code(1);
  b.timeout(hold, b.fixed(10000), done);
  b.on_entry(hold, OutputAction{3, OutputActionKind::High, 0});
  b.entry(hold);
  REQUIRE(validate(b.g) == GraphError::None);

  StateMachine m(b.g);
  uint32_t t = 0;
  const OutputUpdate up = m.start(1, t);
  CHECK(up.set_high == bit(3));

  t += ms(5);
  CHECK(m.force_end(t));
  CHECK_FALSE(m.is_running());
  CHECK(m.get_record().force_ended);
  CHECK(m.get_record().terminal_code == kNotTerminal);

  // Everything the state raised comes down, even though the graph declared no
  // exit action at all.
  const OutputUpdate after = m.advance(0, t + 100);
  CHECK((after.set_low & bit(3)) == bit(3));

  CHECK_FALSE(m.force_end(t + 200));  // the first decision wins
}

TEST_CASE("the run cap stops a graph that validates but never ends") {
  Builder b;
  const uint8_t spin = b.state();
  const uint8_t done = b.terminal_code(1);
  // Reachable on paper, never reached in practice: nothing drives the input.
  b.on(spin, Transition{bit(0), 0, 0, done, kNoRandomDistribution, false});
  b.entry(spin);
  REQUIRE(validate(b.g) == GraphError::None);

  StateMachine m(b.g);
  m.set_run_cap_ms(50);
  uint32_t t = 0;
  m.start(1, t);
  run_until(m, 0, t, ms(80));

  CHECK_FALSE(m.is_running());
  CHECK(m.get_record().hit_run_cap);
  CHECK(m.get_record().terminal_code == kNotTerminal);
}

TEST_CASE("the same seed gives the same run") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t done = b.terminal_code(1);
  b.timeout(wait, b.uniform(10, 200), done);
  b.entry(wait);

  auto run_once = [&](uint64_t seed) {
    StateMachine m(b.g);
    uint32_t t = 0;
    m.start(seed, t);
    run_until(m, 0, t, ms(500));
    return m.get_record().path[0].drawn_ms;
  };

  CHECK(run_once(99) == run_once(99));
}

TEST_CASE("every output action kind has a defined effect on the update") {
  // All four kinds resolve to lines in the two masks, and the HAL is left with
  // "drive these high, drive those low". Pinned because the HAL is written
  // against exactly this and a silent change to any of it moves a real line.
  Builder b;
  const uint8_t drive = b.state();
  const uint8_t done = b.terminal_code(1);
  b.on_entry(drive, OutputAction{0, OutputActionKind::High, 0});
  b.on_entry(drive, OutputAction{1, OutputActionKind::Low, 0});
  b.on_entry(drive, OutputAction{2, OutputActionKind::Toggle, 0});
  b.on_entry(drive, OutputAction{3, OutputActionKind::Pulse, 50});
  b.timeout(drive, b.fixed(10), done);
  b.entry(drive);
  REQUIRE(validate(b.g) == GraphError::None);

  StateMachine m(b.g);
  const OutputUpdate on_entry = m.start(1, 0);
  CHECK((on_entry.set_high & bit(0)) != 0);
  CHECK((on_entry.set_low & bit(0)) == 0);
  CHECK((on_entry.set_low & bit(1)) != 0);
  CHECK((on_entry.set_high & bit(1)) == 0);
  CHECK((on_entry.set_high & bit(2)) != 0);  // Toggle, from low, goes high
  CHECK((on_entry.set_low & bit(2)) == 0);
  CHECK((on_entry.set_high & bit(3)) != 0);  // Pulse rises here, falls on its own

  // Leaving the state lowers what it raised -- and only that. A line the state
  // drove low was never raised, so it is not forced high again on the way out.
  uint32_t t = 0;
  OutputUpdate on_exit;
  while (m.is_running()) {
    t += 100;
    const OutputUpdate u = m.advance(0, t);
    on_exit.set_high |= u.set_high;
    on_exit.set_low |= u.set_low;
  }
  CHECK((on_exit.set_low & bit(0)) != 0);   // High came down
  CHECK((on_exit.set_low & bit(2)) != 0);   // so did the line Toggle raised
  CHECK((on_exit.set_high & bit(1)) == 0);  // Low was not restored
  // The state lasted 10 ms and the pulse asked for 50, so the exit is what
  // lowers it. A pulse is "for at most this long", never a line outliving its
  // state.
  CHECK((on_exit.set_low & bit(3)) != 0);
}

TEST_CASE("a pulse shorter than its state comes down on its own width") {
  Builder b;
  const uint8_t drive = b.state();
  const uint8_t done = b.terminal_code(1);
  b.on_entry(drive, OutputAction{4, OutputActionKind::Pulse, 20});
  b.timeout(drive, b.fixed(500), done);
  b.entry(drive);
  REQUIRE(validate(b.g) == GraphError::None);

  StateMachine m(b.g);
  CHECK((m.start(1, 0).set_high & bit(4)) != 0);

  uint32_t fell_at = 0;
  for (uint32_t t = 100; t <= ms(100); t += 100) {
    if (m.advance(0, t).set_low & bit(4)) {
      fell_at = t;
      break;
    }
  }
  REQUIRE(fell_at != 0);
  // Within one scan of 20 ms, and nowhere near the state's 500 ms timeout.
  CHECK(fell_at >= ms(20));
  CHECK(fell_at <= ms(20) + 100);
  CHECK(m.is_running());
}

TEST_CASE("a reward pulsed on entering a terminal state still comes down") {
  // This is the case the split between advance() and service_outputs() exists
  // for: the run has ended, so advance() returns early, and the valve is open.
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal_code(1);
  b.timeout(wait, b.fixed(10), hit);
  b.on_entry(hit, OutputAction{7, OutputActionKind::Pulse, 30});
  b.entry(wait);
  REQUIRE(validate(b.g) == GraphError::None);

  StateMachine m(b.g);
  m.start(1, 0);
  uint32_t t = 0;
  LineBitmask raised = 0;
  while (m.is_running()) {
    t += 100;
    raised |= m.advance(0, t).set_high;
  }
  REQUIRE((raised & bit(7)) != 0);
  CHECK((m.driven_levels() & bit(7)) != 0);

  // Nothing is running now. service_outputs() is the only thing still called.
  uint32_t fell_at = 0;
  for (uint32_t u = t; u <= t + ms(200); u += 100) {
    if (m.service_outputs(u).set_low & bit(7)) {
      fell_at = u;
      break;
    }
  }
  REQUIRE(fell_at != 0);
  CHECK(fell_at - t >= ms(30) - 100);
  CHECK((m.driven_levels() & bit(7)) == 0);
}

TEST_CASE("toggle is resolved against where the line actually is") {
  Builder b;
  const uint8_t a = b.state();
  const uint8_t done = b.terminal_code(1);
  b.on_entry(a, OutputAction{9, OutputActionKind::Toggle, 0});
  b.timeout(a, b.fixed(10), done);
  b.entry(a);
  REQUIRE(validate(b.g) == GraphError::None);

  StateMachine m(b.g);
  // The pins came out of a fail-safe with line 9 high, and nothing can read a
  // pin back -- so unless the machine is told, the toggle goes the wrong way.
  m.set_initial_levels(bit(9));
  const OutputUpdate u = m.start(1, 0);
  CHECK((u.set_low & bit(9)) != 0);
  CHECK((u.set_high & bit(9)) == 0);
}

TEST_CASE("a pulse with no width is refused rather than never coming down") {
  Builder b;
  const uint8_t a = b.state();
  const uint8_t done = b.terminal_code(1);
  b.on_entry(a, OutputAction{1, OutputActionKind::Pulse, 0});
  b.timeout(a, b.fixed(10), done);
  b.entry(a);
  CHECK(validate(b.g) == GraphError::BadPulse);
}
