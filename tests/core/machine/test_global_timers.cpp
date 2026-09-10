// SPDX-License-Identifier: GPL-3.0-or-later
// The global timers on their own, with no session and no wire.
//
// The bank is driven directly here so that a test can put the clock exactly
// where it wants it and read the bits back, which is the only way to check the
// phase arithmetic. What the session does with them -- the enable mask, the
// per-trial override, the actions -- is in test_host_link_session.cpp.
#include <vector>

#include "doctest.h"
#include "graph/graph_set.h"
#include "machine/global_timers.h"

using namespace statemachined;

namespace {

constexpr Microseconds ms(uint32_t n) { return n * 1000u; }

/// A set with `n` fixed distributions, so a timer can name durations by index.
/// Slot i is (i + 1) * 100 ms, which keeps the arithmetic in a test readable.
GraphSet a_set_with_fixed_distributions(uint8_t n) {
  GraphSet s;
  s.n_distributions = n;
  for (uint8_t i = 0; i < n; ++i) {
    s.distributions[i].kind = RandomDistributionKind::Fixed;
    s.distributions[i].a = static_cast<Milliseconds>((i + 1) * 100);
  }
  return s;
}

constexpr LineBitmask line(uint8_t i) { return static_cast<LineBitmask>(1) << i; }
constexpr LineBitmask t_line(uint8_t i) { return line(timer_line(i)); }

/// Scan from `from` to `until` in 1 ms steps with the world word held at
/// `word`, the way the timer would be ticked on a board. Returns everything the
/// bank asked for on the way.
OutputUpdate scan(GlobalTimerBank& bank, LineBitmask word, Microseconds from,
                  Microseconds until) {
  OutputUpdate all;
  for (Microseconds t = from; t <= until; t += ms(1)) {
    const TimerTick tick = bank.tick(word, t);
    all.set_high = (all.set_high | tick.ops.set_high) & ~tick.ops.set_low;
    all.set_low = (all.set_low | tick.ops.set_low) & ~tick.ops.set_high;
  }
  return all;
}

}  // namespace

TEST_CASE("a timer is an input line that is high while it runs") {
  // The whole design in one assertion. Everything a transition does with a
  // lever it can do with this bit, which is why no new predicate vocabulary
  // exists anywhere for timers.
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 1;
  s.timers[0].width = 1;  // 200 ms
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  CHECK(bank.tick(0, ms(1)).bits == 0);
  bank.start(0, ms(10));
  CHECK(bank.tick(0, ms(11)).bits == t_line(0));
  CHECK(bank.tick(0, ms(200)).bits == t_line(0));
  // 200 ms after the start, not after the first tick that noticed it.
  CHECK(bank.tick(0, ms(210)).bits == 0);
}

TEST_CASE("a timer drives a real output line beside its own bit") {
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 1;
  s.timers[0].width = 1;  // 200 ms
  s.timers[0].output_line = 3;
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  const OutputUpdate up = bank.start(0, ms(10));
  CHECK(up.set_high == line(3));
  CHECK(up.set_low == 0);

  const TimerTick down = bank.tick(0, ms(210));
  CHECK(down.ops.set_low == line(3));
  CHECK(down.bits == 0);
}

TEST_CASE("active_low inverts the pin and not the timer's own bit") {
  // A predicate waiting on a timer must not have to know how the valve is
  // wired. VStim's m_PulsePolarity applies to the output line only, and so does
  // this.
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 1;
  s.timers[0].width = 1;
  s.timers[0].output_line = 3;
  s.timers[0].active_low = true;
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  const OutputUpdate up = bank.start(0, ms(10));
  CHECK(up.set_low == line(3));     // the pin goes down
  CHECK(bank.bits() == t_line(0));  // the bit goes up
  CHECK(bank.tick(0, ms(210)).ops.set_high == line(3));
}

TEST_CASE("an onset delay is served before the timer goes high") {
  GraphSet s = a_set_with_fixed_distributions(3);
  s.n_timers = 1;
  s.timers[0].delay = 2;  // 300 ms
  s.timers[0].width = 0;  // 100 ms
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  bank.start(0, 0);
  CHECK(bank.tick(0, ms(1)).bits == 0);
  CHECK(bank.tick(0, ms(299)).bits == 0);
  CHECK(bank.tick(0, ms(300)).bits == t_line(0));
  CHECK(bank.tick(0, ms(399)).bits == t_line(0));
  CHECK(bank.tick(0, ms(400)).bits == 0);
}

TEST_CASE("loops and a gap make a pulse train") {
  // Valve.cpp's split reward: three periods open with a gap between them, so a
  // total volume is delivered in pieces rather than in one draught. Bpod calls
  // the same thing Loop and LoopInterval.
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 1;
  s.timers[0].width = 0;  // 100 ms open
  s.timers[0].gap = 1;    // 200 ms closed
  s.timers[0].loops = 3;
  s.timers[0].output_line = 2;
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  bank.start(0, 0);
  // open  0..100, gap 100..300, open 300..400, gap 400..600, open 600..700
  struct Sample {
    Microseconds at;
    bool high;
  };
  const Sample expected[] = {
      {ms(50), true},   {ms(150), false}, {ms(250), false}, {ms(350), true},
      {ms(450), false}, {ms(650), true},  {ms(750), false},
  };
  for (const Sample& e : expected) {
    const TimerTick tick = bank.tick(0, e.at);
    CHECK(((tick.bits & t_line(0)) != 0) == e.high);
  }
  // Three pulses and then done, rather than a fourth.
  CHECK(bank.any_running() == false);
}

TEST_CASE("loops of zero runs until something stops it") {
  // VStim's FrequencyGenerator, which has no count to run out.
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 1;
  s.timers[0].width = 0;
  s.timers[0].gap = 0;
  s.timers[0].loops = 0;
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  bank.start(0, 0);
  scan(bank, 0, 0, ms(5000));
  CHECK(bank.any_running());
  bank.cancel(0, ms(5000));
  CHECK(bank.any_running() == false);
  CHECK(bank.bits() == 0);
}

TEST_CASE("a timer starts on its trigger's rising edge, and only on an edge") {
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 1;
  s.timers[0].all_high = line(4);
  s.timers[0].width = 1;  // 200 ms
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  // Already high on the first word this bank ever sees: no edge, because there
  // is no previous word for it to be an edge against.
  CHECK(bank.tick(line(4), ms(1)).bits == 0);
  CHECK(bank.tick(line(4), ms(2)).bits == 0);
  // Released and asserted again is the edge.
  bank.tick(0, ms(3));
  CHECK(bank.tick(line(4), ms(4)).bits == t_line(0));
}

TEST_CASE("a timer with no trigger masks is started only by an action") {
  // All three masks zero is a vacuously true predicate, which would otherwise
  // fire on the first scan. Those timers are simply never evaluated.
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 1;
  s.timers[0].width = 1;
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  scan(bank, 0, 0, ms(50));
  CHECK(bank.bits() == 0);
  bank.start(0, ms(50));
  CHECK(bank.bits() == t_line(0));
}

TEST_CASE("one timer triggers another, on the scan the first one ends") {
  // Bpod's OnsetTrigger and VStim's divider chain, neither of them written:
  // the timers' bits are in the word the triggers are evaluated against, so a
  // timer waiting on another timer is a predicate like any other.
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 2;
  s.timers[0].width = 0;  // 100 ms
  // Timer 1 starts when timer 0 stops. `none_high` on timer 0's line is the
  // falling edge of the predicate "timer 0 is running".
  s.timers[1].none_high = t_line(0);
  s.timers[1].width = 1;  // 200 ms
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  // The first word primes the latch: timer 0 is not running, so timer 1's
  // predicate is already true and must not fire on it.
  CHECK(bank.tick(0, 0).bits == 0);
  bank.start(0, ms(1));
  CHECK(bank.tick(0, ms(2)).bits == t_line(0));
  // Timer 0 ends at 101 ms. Pass one drops its bit; pass two sees the word it
  // left behind and starts timer 1 -- the same scan, not the one after.
  const TimerTick at_end = bank.tick(0, ms(101));
  CHECK((at_end.bits & t_line(0)) == 0);
  CHECK((at_end.bits & t_line(1)) != 0);
}

TEST_CASE("a running timer is not restarted by another trigger") {
  // VStim reads its trigger only in WaitTrigger, and this is why: a state
  // re-entered in a loop would otherwise keep pushing the timer's end further
  // away, and a timer whose end never arrives is a transition that never fires.
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 1;
  s.timers[0].all_high = line(4);
  s.timers[0].width = 1;  // 200 ms
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  bank.tick(0, 0);
  bank.tick(line(4), ms(10));  // edge: starts, due at 210
  bank.tick(0, ms(50));
  bank.tick(line(4), ms(100));  // another edge, mid-pulse
  CHECK(bank.bits() == t_line(0));
  // Still the original deadline, not 100 + 200.
  CHECK(bank.tick(0, ms(209)).bits == t_line(0));
  CHECK(bank.tick(0, ms(210)).bits == 0);
}

TEST_CASE("a disabled timer does not start, however it is asked") {
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 2;
  s.timers[0].width = 1;
  s.timers[1].all_high = line(4);
  s.timers[1].width = 1;
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);
  bank.set_enabled(0, 0);

  bank.start(0, ms(1));  // the action route
  bank.tick(0, ms(2));
  bank.tick(line(4), ms(3));  // the trigger route
  CHECK(bank.bits() == 0);
  CHECK(bank.any_running() == false);
}

TEST_CASE("disabling a running timer stops it and puts its line back") {
  // Not left to finish. A timer holding a line up that the new configuration
  // does not account for is not something a host can plan a trial around.
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 1;
  s.timers[0].width = 1;
  s.timers[0].output_line = 5;
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  bank.start(0, 0);
  CHECK(bank.bits() == t_line(0));
  const OutputUpdate off = bank.set_enabled(0, ms(50));
  CHECK(off.set_low == line(5));
  CHECK(bank.bits() == 0);
}

TEST_CASE("only the trial-bound timers stop when a run ends") {
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 2;
  s.timers[0].width = 1;
  s.timers[0].trial_bound = true;
  s.timers[1].width = 1;
  s.timers[1].trial_bound = false;
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  bank.start(0, 0);
  bank.start(1, 0);
  CHECK(bank.bits() == (t_line(0) | t_line(1)));
  bank.end_of_run(ms(50));
  // The free-running one is still going, which is what lets a timer raise a
  // line while the device sits between trials.
  CHECK(bank.bits() == t_line(1));
}

TEST_CASE("binding a new set puts back whatever the old set's timers were driving") {
  // A set upload is refused while a trial is armed or running, so nothing else
  // is going to lower a line this bank raised.
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 1;
  s.timers[0].width = 1;
  s.timers[0].output_line = 6;
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);
  bank.start(0, 0);

  GraphSet next = a_set_with_fixed_distributions(2);
  const OutputUpdate ops = bank.bind(&next, ms(10));
  CHECK(ops.set_low == line(6));
  CHECK(bank.bits() == 0);
}

TEST_CASE("a timer's draws do not disturb the graph's") {
  // Replay of a trial has to be unaffected by whether the set around it happens
  // to run timers, so the bank draws from its own stream. Two banks on the same
  // session seed agree; that is the property, and it is checked by drawing a
  // random width many times and seeing the two banks stay in step.
  GraphSet s = a_set_with_fixed_distributions(1);
  s.distributions[0].kind = RandomDistributionKind::Uniform;
  s.distributions[0].a = 10;
  s.distributions[0].b = 200;
  s.n_timers = 1;
  s.timers[0].width = 0;
  s.timers[0].loops = 0;

  GlobalTimerBank one, two;
  one.reseed(0x1234);
  two.reseed(0x1234);
  one.bind(&s, 0);
  two.bind(&s, 0);
  one.start(0, 0);
  two.start(0, 0);
  for (Microseconds t = 0; t < ms(3000); t += ms(1)) {
    CHECK(one.tick(0, t).bits == two.tick(0, t).bits);
  }
}

// ------------------------------------------------------------- accuracy ---
//
// A timer's edges land on the scan, so an individual edge is up to one scan
// period late and never early. What these are really about is whether that
// error *accumulates*, which is the difference between a free-running timer
// being usable for an hour and being usable for a minute.
//
// The tick here is deliberately not a divisor of a millisecond. A 10 kHz scan
// on 1 ms durations divides exactly and would hide the whole question; 107 us
// is co-prime with 1000, so every deadline falls between two scans and is
// noticed late by a different amount each time -- which is what a real board
// with a drifting crystal and a jittering ISR looks like.

namespace {

constexpr Microseconds kJitteredTick = 107;

/// Every microsecond at which timer `i` went high, ticking `bank` on the
/// jittered grid out to `until`.
std::vector<Microseconds> rising_edges_of(GlobalTimerBank& bank, uint8_t i,
                                          Microseconds until) {
  std::vector<Microseconds> edges;
  bool was_high = (bank.bits() & t_line(i)) != 0;
  for (Microseconds t = 0; t <= until; t += kJitteredTick) {
    const bool high = (bank.tick(0, t).bits & t_line(i)) != 0;
    if (high && !was_high) edges.push_back(t);
    was_high = high;
  }
  return edges;
}

}  // namespace

TEST_CASE("an edge is late by less than one scan, and never early") {
  GraphSet s = a_set_with_fixed_distributions(2);
  s.n_timers = 1;
  s.timers[0].width = 1;  // 200 ms
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);

  bank.start(0, 0);
  Microseconds fell_at = 0;
  for (Microseconds t = 0; t <= ms(300); t += kJitteredTick) {
    if ((bank.tick(0, t).bits & t_line(0)) == 0) {
      fell_at = t;
      break;
    }
  }
  // Never before the deadline: a timer that ended early would be a duration the
  // graph did not ask for, and no amount of jitter excuses one.
  CHECK(fell_at >= ms(200));
  CHECK(fell_at - ms(200) < kJitteredTick);
}

TEST_CASE("a free-running timer does not drift") {
  // The one that matters. Each edge is late by however long the scan took to
  // notice it, and measuring the next deadline from `now` rather than from the
  // deadline just met would fold that lateness into the schedule and add it up
  // -- so the hundredth edge would be a hundred scans late rather than one.
  // VStim measures from the deadline for the same reason.
  GraphSet s = a_set_with_fixed_distributions(1);
  s.n_timers = 1;
  s.timers[0].width = 0;  // 100 ms high
  s.timers[0].gap = 0;    // 100 ms low -- a 5 Hz square wave
  s.timers[0].loops = 0;  // forever
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);
  bank.start(0, 0);

  const std::vector<Microseconds> edges = rising_edges_of(bank, 0, ms(10000));
  REQUIRE(edges.size() >= 40);
  for (size_t n = 0; n < edges.size(); ++n) {
    // The nth rising edge is due at n * 200 ms after the first one.
    const Microseconds ideal = edges[0] + static_cast<Microseconds>(n) * ms(200);
    const int32_t error = static_cast<int32_t>(edges[n] - ideal);
    // Bounded by one tick, not by n ticks. Measured against the arithmetic this
    // replaced -- next deadline from `now` rather than from the deadline met --
    // this fails from n = 2 and the error grows by about 90 us per cycle,
    // monotonically, reaching 4320 us by the forty-eighth edge. 47 of the 48
    // checks fail. That is the shape of the bug: not a wrong edge, a wrong
    // *rate*, which is invisible in any test short enough to eyeball.
    CHECK(error > -static_cast<int32_t>(kJitteredTick));
    CHECK(error < static_cast<int32_t>(kJitteredTick));
  }
}

TEST_CASE("a pulse train delivers its whole width, however it is split") {
  // Valve.cpp's reason for splitting a reward at all: the total open time is
  // the volume, so twenty pulses of 100 ms must be two seconds of open valve
  // and not two seconds plus twenty scans.
  GraphSet s = a_set_with_fixed_distributions(1);
  s.n_timers = 1;
  s.timers[0].width = 0;  // 100 ms
  s.timers[0].gap = 0;    // 100 ms
  s.timers[0].loops = 20;
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);
  bank.start(0, 0);

  Microseconds high_for = 0;
  Microseconds ended_at = 0;
  for (Microseconds t = 0; t <= ms(8000); t += kJitteredTick) {
    if ((bank.tick(0, t).bits & t_line(0)) != 0) high_for += kJitteredTick;
    if (!bank.any_running() && ended_at == 0 && t > 0) ended_at = t;
  }
  // Twenty pulses of 100 ms. The tolerance is the sampling, not the timer: this
  // counts ticks, so it can be out by one tick per pulse either way.
  const Microseconds ideal_high = ms(20 * 100);
  CHECK(high_for > ideal_high - 20 * kJitteredTick);
  CHECK(high_for < ideal_high + 20 * kJitteredTick);
  // And the whole train is 20 highs + 19 gaps, not 20 of each: the last pulse
  // ends the timer rather than opening another gap.
  CHECK(ended_at >= ms(3900));
  CHECK(ended_at < ms(3900) + 40 * kJitteredTick);
}

TEST_CASE("a timer stalled past a whole cycle resyncs rather than catching up") {
  // A long ISR stall, or a board held off. Emitting a burst of truncated pulses
  // to make up ones nobody saw is worse than losing them, so the schedule
  // restarts from now and the missed cycles are dropped.
  GraphSet s = a_set_with_fixed_distributions(1);
  s.n_timers = 1;
  s.timers[0].width = 0;  // 100 ms
  s.timers[0].gap = 0;
  s.timers[0].loops = 0;
  GlobalTimerBank bank;
  bank.reseed(1);
  bank.bind(&s, 0);
  bank.start(0, 0);

  // Nothing ticks for two whole seconds -- ten cycles missed.
  bank.tick(0, ms(2000));

  // Over the next second the timer runs at its declared rate again: five
  // rising edges for a 5 Hz square wave, not the ten it "owes" delivered as a
  // burst of instantaneous ones.
  Microseconds rose_at = 0, fell_at = 0;
  int rising_edges = 0;
  bool was_high = (bank.bits() & t_line(0)) != 0;
  for (Microseconds t = ms(2000); t <= ms(3000); t += kJitteredTick) {
    const bool high = (bank.tick(0, t).bits & t_line(0)) != 0;
    if (high && !was_high) {
      ++rising_edges;
      if (rose_at == 0) rose_at = t;
    }
    if (!high && was_high && rose_at != 0 && fell_at == 0) fell_at = t;
    was_high = high;
  }
  CHECK(rising_edges <= 6);
  CHECK(rising_edges >= 4);
  // And the first pulse after the stall is a full-width one, not a truncated
  // remnant of the cycle that was missed.
  REQUIRE(fell_at > rose_at);
  CHECK(fell_at - rose_at >= ms(100));
  CHECK(fell_at - rose_at < ms(100) + 2 * kJitteredTick);
}
