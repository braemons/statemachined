// SPDX-License-Identifier: GPL-3.0-or-later
// The demo graph is meant to be watched on a bench, which is exactly the kind
// of thing that rots: nobody notices it is broken until they have a board in
// one hand and a jumper wire in the other. So it is run here, on the host, at
// host speed, through the same TrialRunner the board uses.
#include "../helpers.h"
#include "demo/demo_graph.h"
#include "doctest.h"
#include "trial/trial_runner.h"

using namespace statemachined;
using statemachined::test::bit;
using statemachined::test::ms;

namespace {

constexpr uint64_t kSeed = 0xF00DFACEULL;

/// Run the machine forward to `until_us`, feeding it a constant input word, and
/// hand back the levels the engine believes it is driving.
LineBitmask settle(TrialRunner& r, LineBitmask word, Microseconds& now, Microseconds until_us) {
  while (now < until_us) {
    now += 1000;  // 1 kHz here; the board scans at 10 kHz and neither cares
    r.advance(word, now);
  }
  return r.driven_levels();
}

}  // namespace

TEST_CASE("the demo graph is a valid graph") {
  StateGraph g;
  demo::build(g);
  CHECK(validate(g) == GraphError::None);
}

TEST_CASE("the demo graph fits the reference board's capacities") {
  StateGraph g;
  demo::build(g);
  // It has to fit the smallest board we ship, or the bench it exists for is the
  // one place it does not run.
  CHECK(g.n_states <= kMaxStates);
  CHECK(g.n_transitions <= kMaxTransitions);
  CHECK(g.n_output_actions <= kMaxOutputActions);
  CHECK(g.n_distributions <= kMaxDistributions);
  // Every line it names must be one the Uno R4 Minima actually has: 8 in, 8 out.
  CHECK(demo::kStartInput < 8);
  CHECK(demo::kAbortInput < 8);
  CHECK(demo::kFirstStepOutput + demo::kStepCount <= 8);
  CHECK(demo::kReadyOutput < 8);
}

TEST_CASE("it waits for the start switch rather than starting on its own") {
  StateGraph g;
  demo::build(g);
  TrialRunner r(g);

  Microseconds now = ms(1);
  r.start(1, kSeed, now, 0);

  // The ready lamp is lit on entry, and it stays that way.
  CHECK((r.driven_levels() & bit(demo::kReadyOutput)) != 0);
  const LineBitmask idle = settle(r, 0, now, ms(5000));
  CHECK(r.running());
  CHECK((idle & bit(demo::kReadyOutput)) != 0);
  // Five seconds in, no step LED has come on by itself.
  for (uint8_t i = 0; i < demo::kStepCount; ++i)
    CHECK((idle & bit(demo::kFirstStepOutput + i)) == 0);
}

TEST_CASE("the switch starts one LED walking across the step outputs") {
  StateGraph g;
  demo::build(g);
  TrialRunner r(g);

  Microseconds now = ms(1);
  r.start(1, kSeed, now, 0);
  settle(r, 0, now, ms(100));

  // Press and hold past the debounce. The press is a rising edge on line 0.
  const Microseconds pressed_at = now;
  settle(r, bit(demo::kStartInput), now, pressed_at + ms(50));
  CHECK((r.driven_levels() & bit(demo::kReadyOutput)) == 0);
  CHECK((r.driven_levels() & bit(demo::kFirstStepOutput)) != 0);

  // Release, then walk the clock through the chase. Sample in the middle of
  // each 500 ms step, where exactly one step LED must be lit.
  const Microseconds chase_start = now;
  for (uint8_t i = 0; i < demo::kStepCount; ++i) {
    const Microseconds mid = chase_start + ms(500) * i + ms(250);
    const LineBitmask lit = settle(r, 0, now, mid);
    LineBitmask steps = 0;
    for (uint8_t k = 0; k < demo::kStepCount; ++k) steps |= bit(demo::kFirstStepOutput + k);
    CAPTURE(i);
    CHECK((lit & steps) == bit(demo::kFirstStepOutput + i));
  }

  // After the fifth step times out the trial is over, and it is a Hit.
  settle(r, 0, now, chase_start + ms(500) * demo::kStepCount + ms(100));
  CHECK_FALSE(r.running());
  CHECK(r.result().outcome == TrialOutcome::Hit);
}

TEST_CASE("the abort switch ends the trial mid-chase") {
  StateGraph g;
  demo::build(g);
  TrialRunner r(g);

  Microseconds now = ms(1);
  r.start(1, kSeed, now, 0);
  settle(r, 0, now, ms(100));
  settle(r, bit(demo::kStartInput), now, now + ms(50));
  settle(r, 0, now, now + ms(600));  // somewhere in step 1

  CHECK(r.running());
  settle(r, bit(demo::kAbortInput), now, now + ms(50));

  CHECK_FALSE(r.running());
  CHECK(r.result().outcome == TrialOutcome::Cancelled);
  // The abort lamp (the first step output) is the only step LED left on, so a
  // board that stopped looks different from a board still running -- and a
  // chase LED stuck high from the step it was aborted in would fail this.
  LineBitmask steps = 0;
  for (uint8_t k = 0; k < demo::kStepCount; ++k) steps |= bit(demo::kFirstStepOutput + k);
  CHECK((r.driven_levels() & steps) == bit(demo::kFirstStepOutput));
}
