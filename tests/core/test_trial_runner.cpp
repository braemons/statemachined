// The engine is a pure function of (graph, seed, input word, time), which is
// what lets these tests drive whole trials with no board, no clock and no I/O.
#include "doctest.h"
#include "helpers.h"
#include "trial_runner.h"

using namespace fsmd;
using namespace fsmd::test;

namespace {
/// Run the engine forward to `until_us` in 100 us steps -- the real 10 kHz scan
/// period -- holding the input word steady.
void advance(TrialRunner& e, uint32_t word, uint32_t& t, uint32_t until_us) {
  while (t < until_us && e.running()) {
    t += 100;
    e.scan(word, t);
  }
}
}  // namespace

TEST_CASE("a timeout carries the trial to its terminal state") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t ns = b.terminal(TrialOutcome::NotStarted);
  b.timeout(wait, b.fixed(500), ns);
  b.g.entry = wait;
  REQUIRE(validate(b.g) == GraphError::None);

  TrialRunner e(b.g);
  uint32_t t = 0;
  e.start(1, 0, t);
  CHECK(e.running());

  advance(e, 0, t, ms(400));
  CHECK(e.running());  // not yet
  advance(e, 0, t, ms(600));
  CHECK_FALSE(e.running());
  CHECK(e.result().outcome == TrialOutcome::NotStarted);
}

TEST_CASE("a single-line transition fires on its rising edge") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  const uint8_t late = b.terminal(TrialOutcome::Late);
  b.timeout(wait, b.fixed(1000), late);
  Transition c;
  c.all_high = bit(0);
  c.target_state = hit;
  b.on(wait, c);
  b.g.entry = wait;
  REQUIRE(validate(b.g) == GraphError::None);

  TrialRunner e(b.g);
  uint32_t t = 0;
  e.start(1, 42, t);
  advance(e, 0, t, ms(200));
  CHECK(e.running());
  t += 100;
  e.scan(bit(0), t);
  CHECK_FALSE(e.running());
  CHECK(e.result().outcome == TrialOutcome::Hit);
}

TEST_CASE("a transition already true at entry does not fire unless level") {
  // "Wait for the press", not "wait until held". This is the distinction the
  // `level` flag exists for, and getting it backwards would make a lever the
  // animal is already holding end the trial instantly.
  auto build = [](bool fire_if_true_on_entry, Builder& b) {
    const uint8_t wait = b.state();
    const uint8_t hit = b.terminal(TrialOutcome::Hit);
    const uint8_t late = b.terminal(TrialOutcome::Late);
    b.timeout(wait, b.fixed(1000), late);
    Transition c;
    c.all_high = bit(0);
    c.target_state = hit;
    c.fire_if_true_on_entry = fire_if_true_on_entry;
    b.on(wait, c);
    b.g.entry = wait;
  };

  SUBCASE("edge semantics wait for the line to fall first") {
    Builder b;
    build(false, b);
    TrialRunner e(b.g);
    uint32_t t = 0;
    e.start(1, 1, t, bit(0));        // the lever is ALREADY down at entry
    advance(e, bit(0), t, ms(300));  // still down: no fire
    CHECK(e.running());
    t += 100;
    e.scan(0, t);  // release arms it
    t += 100;
    e.scan(bit(0), t);  // press fires it
    CHECK_FALSE(e.running());
    CHECK(e.result().outcome == TrialOutcome::Hit);
  }

  SUBCASE("level semantics fire immediately") {
    Builder b;
    build(true, b);
    TrialRunner e(b.g);
    uint32_t t = 0;
    e.start(1, 1, t, bit(0));  // already down, and `level` fires anyway
    t += 100;
    e.scan(bit(0), t);
    CHECK_FALSE(e.running());
    CHECK(e.result().outcome == TrialOutcome::Hit);
  }
}

TEST_CASE("a line that rises between arming and the first scan is an edge") {
  // The distinction the previous test rests on: "already true at entry" is a
  // fact about the word at entry, not an assumption. A lever pressed in the
  // gap between arming and the first scan has genuinely gone up.
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  const uint8_t late = b.terminal(TrialOutcome::Late);
  b.timeout(wait, b.fixed(1000), late);
  Transition c;
  c.all_high = bit(0);
  c.target_state = hit;
  b.on(wait, c);
  b.g.entry = wait;

  TrialRunner e(b.g);
  uint32_t t = 0;
  e.start(1, 1, t, 0);  // low at entry
  t += 100;
  e.scan(bit(0), t);  // high on the very first scan: fires
  CHECK_FALSE(e.running());
  CHECK(e.result().outcome == TrialOutcome::Hit);
}

TEST_CASE("a combination of TTL lines is one transition") {
  // The thing Bpod cannot express: its Transition is one channel and one value.
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  const uint8_t late = b.terminal(TrialOutcome::Late);
  b.timeout(wait, b.fixed(5000), late);
  Transition c;
  c.all_high = bit(0) | bit(1);  // both levers
  c.none_high = bit(2);          // and the abort line low
  c.target_state = hit;
  b.on(wait, c);
  b.g.entry = wait;
  REQUIRE(validate(b.g) == GraphError::None);

  TrialRunner e(b.g);
  uint32_t t = 0;
  e.start(1, 1, t);

  t += 100;
  e.scan(bit(0), t);  // one lever: no
  CHECK(e.running());
  t += 100;
  e.scan(bit(0) | bit(1) | bit(2), t);  // both, but abort high: no
  CHECK(e.running());
  t += 100;
  e.scan(bit(0) | bit(1), t);  // both, abort low: fire
  CHECK_FALSE(e.running());
  CHECK(e.result().outcome == TrialOutcome::Hit);
}

TEST_CASE("any-of fires on whichever line arrives") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  const uint8_t late = b.terminal(TrialOutcome::Late);
  b.timeout(wait, b.fixed(5000), late);
  Transition c;
  c.any_high = bit(3) | bit(4);
  c.target_state = hit;
  b.on(wait, c);
  b.g.entry = wait;

  TrialRunner e(b.g);
  uint32_t t = 0;
  e.start(1, 1, t);
  t += 100;
  e.scan(bit(4), t);
  CHECK_FALSE(e.running());
  CHECK(e.result().outcome == TrialOutcome::Hit);
}

TEST_CASE("hold_ms requires the predicate to stay true") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  const uint8_t late = b.terminal(TrialOutcome::Late);
  b.timeout(wait, b.fixed(5000), late);
  const uint8_t hold = b.fixed(200);
  Transition c;
  c.all_high = bit(0) | bit(1);
  c.target_state = hit;
  c.hold_duration = hold;
  b.on(wait, c);
  b.g.entry = wait;

  TrialRunner e(b.g);
  uint32_t t = 0;
  e.start(1, 1, t);

  SUBCASE("a dropout restarts the hold") {
    advance(e, bit(0) | bit(1), t, ms(150));
    CHECK(e.running());
    t += 100;
    e.scan(0, t);                             // dropped
    advance(e, bit(0) | bit(1), t, ms(300));  // only 150 ms of new hold
    CHECK(e.running());
    advance(e, bit(0) | bit(1), t, ms(600));
    CHECK_FALSE(e.running());
    CHECK(e.result().outcome == TrialOutcome::Hit);
  }

  SUBCASE("an uninterrupted hold fires, on a steady word") {
    // Also the regression test for the change-detection optimisation: the input
    // word never changes during the hold, so a scan that skipped evaluation on
    // an unchanged word would never fire this.
    advance(e, bit(0) | bit(1), t, ms(250));
    CHECK_FALSE(e.running());
    CHECK(e.result().outcome == TrialOutcome::Hit);
  }
}

TEST_CASE("declaration order resolves a tie") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t first = b.terminal(TrialOutcome::Hit);
  const uint8_t second = b.terminal(TrialOutcome::WrongResponse);
  const uint8_t late = b.terminal(TrialOutcome::Late);
  b.timeout(wait, b.fixed(5000), late);
  Transition a;
  a.all_high = bit(0);
  a.target_state = first;
  b.on(wait, a);
  Transition c;
  c.any_high = bit(0) | bit(1);
  c.target_state = second;
  b.on(wait, c);
  b.g.entry = wait;

  TrialRunner e(b.g);
  uint32_t t = 0;
  e.start(1, 1, t);
  t += 100;
  e.scan(bit(0), t);  // both transitions hold on this scan
  CHECK(e.result().outcome == TrialOutcome::Hit);
}

TEST_CASE("outputs a state raised come down when it is left") {
  // The engine guarantees this rather than trusting the graph to say so. A
  // valve left open because a graph forgot an on_exit is not acceptable.
  Builder b;
  const uint8_t reward = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.on_entry(reward, OutputAction{2, OutputActionKind::High, 0});  // the valve
  b.timeout(reward, b.fixed(100), hit);
  b.g.entry = reward;
  REQUIRE(validate(b.g) == GraphError::None);

  TrialRunner e(b.g);
  uint32_t t = 0;
  const OutputUpdate open = e.start(1, 1, t);
  CHECK((open.set_high & bit(2)) != 0);

  uint32_t lowered = 0;
  while (e.running()) {
    t += 100;
    lowered |= e.scan(0, t).set_low;
  }
  CHECK((lowered & bit(2)) != 0);
}

TEST_CASE("cancel lowers the outputs and names CANCELLED") {
  Builder b;
  const uint8_t reward = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.on_entry(reward, OutputAction{2, OutputActionKind::High, 0});
  b.timeout(reward, b.fixed(10000), hit);
  b.g.entry = reward;

  TrialRunner e(b.g);
  uint32_t t = 0;
  const OutputUpdate open = e.start(7, 1, t);
  CHECK((open.set_high & bit(2)) != 0);

  t += ms(50);
  CHECK(e.cancel(TrialCancelReason::Host, t));
  CHECK_FALSE(e.running());
  CHECK(e.result().outcome == TrialOutcome::Cancelled);
  CHECK(e.result().cancel_reason == TrialCancelReason::Host);
  CHECK(e.result().trial_id == 7);

  const OutputUpdate after = e.scan(0, t + 100);
  CHECK((after.set_low & bit(2)) != 0);  // the valve closes
}

TEST_CASE("the first terminal decision wins") {
  // A cancel arriving after the animal already responded must return the real
  // outcome, not a fabricated CANCELLED. triald has to cope with asking to
  // cancel and being told HIT; the alternative is a record that lies.
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  const uint8_t late = b.terminal(TrialOutcome::Late);
  b.timeout(wait, b.fixed(5000), late);
  Transition c;
  c.all_high = bit(0);
  c.target_state = hit;
  b.on(wait, c);
  b.g.entry = wait;

  TrialRunner e(b.g);
  uint32_t t = 0;
  e.start(1, 1, t);
  t += 100;
  e.scan(bit(0), t);
  REQUIRE(e.result().outcome == TrialOutcome::Hit);

  CHECK_FALSE(e.cancel(TrialCancelReason::Host, t + 100));
  CHECK(e.result().outcome == TrialOutcome::Hit);
  CHECK(e.result().cancel_reason == TrialCancelReason::None);
}

TEST_CASE("the trial cap catches a graph that cannot end") {
  // Validation proves a terminal state is reachable. It cannot prove one is
  // reached: a transition that never becomes true is indistinguishable from a
  // long foreperiod. The cap is why an unreachable exit cannot hang the rig.
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  Transition c;
  c.all_high = bit(9);  // never raised in this test
  c.target_state = hit;
  b.on(wait, c);
  b.g.entry = wait;
  REQUIRE(validate(b.g) == GraphError::None);  // it *looks* fine

  TrialRunner e(b.g);
  e.set_trial_cap_ms(1000);
  uint32_t t = 0;
  e.start(1, 1, t);
  advance(e, 0, t, ms(2000));
  CHECK_FALSE(e.running());
  CHECK(e.result().outcome == TrialOutcome::Cancelled);
  CHECK(e.result().cancel_reason == TrialCancelReason::TrialTimeout);
}

TEST_CASE("a self-transition resets the timer and redraws the duration") {
  // Bpod detects transitions with `NewState != CurrentState`, which silently
  // makes a self-loop a no-op. A re-triggerable timeout should be expressible.
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t ns = b.terminal(TrialOutcome::NotStarted);
  b.timeout(wait, b.fixed(500), ns);
  Transition c;
  c.all_high = bit(0);
  c.target_state = wait;  // back to itself
  b.on(wait, c);
  b.g.entry = wait;
  REQUIRE(validate(b.g) == GraphError::None);

  TrialRunner e(b.g);
  uint32_t t = 0;
  e.start(1, 1, t);

  // Poke the line every 300 ms: the 500 ms timeout must never elapse.
  for (int i = 0; i < 6; ++i) {
    advance(e, 0, t, t + ms(300));
    REQUIRE(e.running());
    t += 100;
    e.scan(bit(0), t);
    t += 100;
    e.scan(0, t);
  }
  CHECK(e.running());
  CHECK(e.run().path_len >= 6);  // each self-transition is recorded

  advance(e, 0, t, t + ms(700));
  CHECK_FALSE(e.running());
  CHECK(e.result().outcome == TrialOutcome::NotStarted);
}

TEST_CASE("the path records every state with its realised duration") {
  Builder b;
  const uint8_t a = b.state();
  const uint8_t c = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.timeout(a, b.fixed(200), c);
  b.timeout(c, b.fixed(300), hit);
  b.g.entry = a;

  TrialRunner e(b.g);
  uint32_t t = 0;
  e.start(1, 1, t);
  advance(e, 0, t, ms(1000));
  CHECK_FALSE(e.running());

  const StateMachineRunRecord& r = e.run();
  REQUIRE(r.path_len >= 2);
  CHECK(r.path[0].state_index == a);
  CHECK(r.path[0].drawn_ms == 200);
  CHECK(r.path[0].duration_us >= ms(200));
  CHECK(r.path[0].duration_us < ms(201));
  CHECK(r.path[1].state_index == c);
  CHECK(r.path[1].drawn_ms == 300);
  CHECK(r.path[1].cause == StateExitCause::Timeout);
  CHECK(r.total_us >= ms(500));
}

TEST_CASE("a randomised duration is reported and is reproducible") {
  // The seed makes it reproducible; the report makes it evidence. Both, because
  // a record has to show what actually happened.
  Builder b;
  const uint8_t fore = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.timeout(fore, b.uniform(200, 800), hit);
  b.g.entry = fore;

  auto run = [&](uint32_t trial_id) {
    TrialRunner e(b.g);
    uint32_t t = 0;
    e.start(trial_id, 0xC0FFEE, t);
    advance(e, 0, t, ms(2000));
    return e.run().path[0].drawn_ms;
  };

  const int32_t a1 = run(412);
  const int32_t a2 = run(412);
  CHECK(a1 == a2);
  CHECK(a1 >= 200);
  CHECK(a1 <= 800);

  bool differs = false;
  for (uint32_t id = 413; id < 425 && !differs; ++id) differs = (run(id) != a1);
  CHECK(differs);
}

TEST_CASE("the path truncates rather than corrupts") {
  Builder b;
  const uint8_t loop = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.timeout(loop, b.fixed(1), loop);  // self-loop every 1 ms
  Transition c;
  c.all_high = bit(0);
  c.target_state = hit;
  b.on(loop, c);
  b.g.entry = loop;

  TrialRunner e(b.g);
  uint32_t t = 0;
  e.start(1, 1, t);
  advance(e, 0, t, ms(500));  // ~500 visits into a 64-entry buffer

  CHECK(e.run().path_len <= kMaxPath);
  CHECK(e.run().path_truncated);
}

TEST_CASE("micros() wraparound does not disturb a trial") {
  // A 32-bit microsecond counter wraps every ~71 minutes. A trial must not care.
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t ns = b.terminal(TrialOutcome::NotStarted);
  b.timeout(wait, b.fixed(500), ns);
  b.g.entry = wait;

  TrialRunner e(b.g);
  uint32_t t = 0xFFFFFFFF - ms(200);  // wraps mid-trial
  e.start(1, 1, t);
  for (int i = 0; i < 8000 && e.running(); ++i) {
    t += 100;
    e.scan(0, t);
  }
  CHECK_FALSE(e.running());
  CHECK(e.result().outcome == TrialOutcome::NotStarted);
  CHECK(e.run().path[0].duration_us >= ms(500));
  CHECK(e.run().path[0].duration_us < ms(501));
}
