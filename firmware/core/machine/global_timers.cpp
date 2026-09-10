// SPDX-License-Identifier: GPL-3.0-or-later
#include "machine/global_timers.h"

#include "graph/transition.h"

namespace statemachined {
namespace {

/// Has `deadline` passed? Signed difference, so it stays right across the
/// ~71-minute wrap of a 32-bit microsecond counter. The same helper, and the
/// same reasoning, as state_machine.cpp's.
inline bool reached(Microseconds deadline, Microseconds now) {
  return static_cast<int32_t>(now - deadline) >= 0;
}

/// Merge `later` over `earlier`, per line. Not two ORs: within one scan a timer
/// can end and another start on the same output line, and the answer is the
/// later one, not both.
inline void merge(OutputUpdate& earlier, const OutputUpdate& later) {
  earlier.set_high = (earlier.set_high | later.set_high) & ~later.set_low;
  earlier.set_low = (earlier.set_low | later.set_low) & ~later.set_high;
}

/// The next deadline, measured from the one just met rather than from the scan
/// that noticed it.
///
/// A scan notices a deadline up to one scan period late. Scheduling the next
/// phase from `now_us` therefore adds that lateness to every phase and
/// **accumulates** it: a free-running 10 Hz timer would lose up to a scan
/// period per cycle and be measurably slow within a minute, and a split reward
/// of twenty pulses would run twenty periods long. Measuring from the previous
/// deadline instead makes the error bounded rather than cumulative -- an
/// individual edge is still up to one period late, but the Nth edge is not N
/// periods late. VStim does this and says so: `m_NextPulse_HR += m_Periode_HR`.
///
/// The exception is falling a whole phase behind -- a long stall, or a board
/// held off. Catching up would mean emitting a burst of truncated pulses to
/// make up ones nobody saw, which is worse than losing them, so the schedule
/// resyncs to now and the cycles in between are dropped.
inline Microseconds next_deadline(Microseconds previous_due, Microseconds now_us,
                                  Milliseconds duration_ms) {
  const Microseconds span = static_cast<Microseconds>(duration_ms) * 1000u;
  const Microseconds accumulated = previous_due + span;
  return reached(accumulated, now_us) ? now_us + span : accumulated;
}

/// The trigger predicate, which is a Transition's and is evaluated by
/// Transition's own code. Borrowing the type rather than the three lines keeps
/// the two from drifting: a fourth mask added to transitions would otherwise
/// silently not apply to timers.
inline bool trigger_holds(const GlobalTimer& t, LineBitmask w) {
  Transition guard;
  guard.all_high = t.all_high;
  guard.any_high = t.any_high;
  guard.none_high = t.none_high;
  return guard.holds(w);
}

/// A timer with no masks at all has a vacuously true predicate, which would
/// fire it on the first edge-less scan. Those are the timers started only by a
/// TimerStart action, so the trigger is simply never evaluated for them.
inline bool has_trigger(const GlobalTimer& t) {
  return t.all_high != 0 || t.any_high != 0 || t.none_high != 0;
}

}  // namespace

void GlobalTimerBank::reseed(uint64_t session_seed) {
  // Salted, not `Rng::mix(seed, trial_id)`: the timers are device-scope and
  // have no trial to derive from, and mixing against a real trial id would tie
  // their stream to whichever trial happened to be running when they were
  // seeded. XOR with a constant keeps them one stream per session, distinct
  // from every trial's, so a graph's own draws are identical whether or not the
  // set declares timers.
  rng_.reseed(session_seed ^ 0x54494D4552534D64ull);  // "TIMERSMd"
}

OutputUpdate GlobalTimerBank::bind(const GraphSet* set, Microseconds now_us) {
  OutputUpdate ops;
  // Put back whatever was driven before forgetting that it was. A set upload is
  // refused while a trial is armed or running, so nothing else is going to
  // lower a line this bank raised.
  for (uint8_t i = 0; i < kMaxTimers; ++i) {
    if (rt_[i].phase == Phase::High) merge(ops, drive(i, false));
    rt_[i] = Runtime{};
  }
  set_ = set;
  bits_ = 0;
  armed_ = 0;
  last_word_ = 0;
  have_word_ = false;
  (void)now_us;
  return ops;
}

Milliseconds GlobalTimerBank::draw(RandomDistributionIndex d) {
  if (set_ == nullptr || d == kNoRandomDistribution || d >= set_->n_distributions) return 0;
  const Milliseconds ms = set_->distributions[d].draw(rng_);
  return ms < 0 ? 0 : ms;
}

OutputUpdate GlobalTimerBank::drive(uint8_t i, bool on) {
  OutputUpdate ops;
  const GlobalTimer& t = set_->timers[i];
  if (t.output_line == kNoLine || t.output_line >= kMaxOutputLines) return ops;
  const LineBitmask bit = static_cast<LineBitmask>(1) << t.output_line;
  // `active_low` inverts the pin and not the timer's own bit: a predicate that
  // waits on the timer should not have to know how the valve is wired. VStim's
  // m_PulsePolarity does the same, on the output line only.
  if (on != t.active_low)
    ops.set_high = bit;
  else
    ops.set_low = bit;
  return ops;
}

OutputUpdate GlobalTimerBank::begin_pulse(uint8_t i, Microseconds now_us,
                                          Microseconds from_us) {
  Runtime& r = rt_[i];
  const GlobalTimer& t = set_->timers[i];
  r.phase = Phase::High;
  r.due_us = next_deadline(from_us, now_us, draw(t.width));
  bits_ |= static_cast<LineBitmask>(1) << timer_line(i);
  return drive(i, true);
}

OutputUpdate GlobalTimerBank::end_pulse(uint8_t i, Microseconds now_us) {
  Runtime& r = rt_[i];
  const GlobalTimer& t = set_->timers[i];
  OutputUpdate ops = drive(i, false);
  bits_ &= ~(static_cast<LineBitmask>(1) << timer_line(i));
  // The deadline this pulse ended on, captured before anything overwrites it.
  // Every phase after it is measured from here rather than from `now_us`.
  const Microseconds ended_at = r.due_us;

  // One pulse is spent. `forever` is VStim's FrequencyGenerator, which has no
  // count to run out.
  if (!r.forever && r.loops_left > 0) --r.loops_left;
  if (!r.forever && r.loops_left == 0) {
    r.phase = Phase::Idle;
    armed_ &= ~(static_cast<LineBitmask>(1) << i);
    return ops;
  }

  const Milliseconds gap_ms = draw(t.gap);
  if (gap_ms <= 0) {
    // No gap declared, so the next pulse begins on this same scan. A width and
    // no gap is a line that never drops, which is what the graph asked for.
    merge(ops, begin_pulse(i, now_us, ended_at));
    return ops;
  }
  r.phase = Phase::Gap;
  r.due_us = next_deadline(ended_at, now_us, gap_ms);
  return ops;
}

OutputUpdate GlobalTimerBank::start(uint8_t i, Microseconds now_us) {
  OutputUpdate ops;
  if (set_ == nullptr || i >= kMaxTimers || i >= set_->n_timers) return ops;
  // A disabled timer does not start, however it was asked. This is the one
  // check, so the `timers` command, a `configure` override and a TimerStart
  // action cannot disagree about what enabled means.
  if ((enabled_ & (1u << i)) == 0) return ops;
  Runtime& r = rt_[i];
  // Already running: ignored, not restarted. VStim reads its trigger only in
  // WaitTrigger for the same reason -- a state re-entered in a loop would
  // otherwise keep pushing the same timer's end further away, and a timer whose
  // end never arrives is a transition that never fires.
  if (r.phase != Phase::Idle) return ops;

  const GlobalTimer& t = set_->timers[i];
  r.forever = (t.loops == 0);
  r.loops_left = t.loops;
  armed_ |= static_cast<LineBitmask>(1) << i;

  const Milliseconds delay_ms = draw(t.delay);
  // A fresh start has no previous deadline to measure from -- the trigger is
  // the origin, and the scan carrying it is as close to the trigger as this
  // device gets.
  if (delay_ms <= 0) return begin_pulse(i, now_us, now_us);
  r.phase = Phase::Delay;
  r.due_us = now_us + static_cast<Microseconds>(delay_ms) * 1000u;
  return ops;
}

OutputUpdate GlobalTimerBank::cancel(uint8_t i, Microseconds now_us) {
  OutputUpdate ops;
  if (set_ == nullptr || i >= kMaxTimers) return ops;
  Runtime& r = rt_[i];
  if (r.phase == Phase::Idle) return ops;
  if (r.phase == Phase::High) ops = drive(i, false);
  bits_ &= ~(static_cast<LineBitmask>(1) << timer_line(i));
  armed_ &= ~(static_cast<LineBitmask>(1) << i);
  // The latch is deliberately left alone. A cancel does not re-arm the trigger:
  // if the predicate that started this timer is still true, cancelling must not
  // hand it straight back on the next scan.
  const bool latch = r.trigger_was_true;
  r = Runtime{};
  r.trigger_was_true = latch;
  (void)now_us;
  return ops;
}

OutputUpdate GlobalTimerBank::set_enabled(uint32_t mask, Microseconds now_us) {
  OutputUpdate ops;
  const uint32_t turned_off = enabled_ & ~mask;
  enabled_ = mask;
  if (turned_off == 0 || set_ == nullptr) return ops;
  // Stopped, not left to finish. A timer that was running when its bit was
  // cleared is holding a line up that the new configuration does not account
  // for, and "it will drop in another 400 ms" is not something a host can plan
  // a trial around.
  for (uint8_t i = 0; i < kMaxTimers; ++i) {
    if ((turned_off & (1u << i)) == 0) continue;
    merge(ops, cancel(i, now_us));
  }
  return ops;
}

OutputUpdate GlobalTimerBank::end_of_run(Microseconds now_us) {
  OutputUpdate ops;
  if (set_ == nullptr) return ops;
  for (uint8_t i = 0; i < set_->n_timers && i < kMaxTimers; ++i) {
    if (!set_->timers[i].trial_bound) continue;
    merge(ops, cancel(i, now_us));
  }
  return ops;
}

TimerTick GlobalTimerBank::tick(LineBitmask world_word, Microseconds now_us) {
  TimerTick out;
  if (set_ == nullptr || set_->n_timers == 0) {
    out.bits = 0;
    return out;
  }
  const uint8_t n = set_->n_timers < kMaxTimers ? set_->n_timers : kMaxTimers;

  // Pass one: the clock. Every timer that is running gets its phase advanced,
  // so a width that has elapsed drops its bit *before* anything looks at the
  // word. A timer chained to this one therefore sees the end on the same scan.
  //
  // Skipped whole when nothing is running, which is most scans of most
  // sessions: `armed_` is the question "is there anything to advance" answered
  // in one compare rather than in kMaxTimers switch statements.
  if (armed_ != 0) {
    for (uint8_t i = 0; i < n; ++i) {
      if ((armed_ & (static_cast<LineBitmask>(1) << i)) == 0) continue;
      Runtime& r = rt_[i];
      switch (r.phase) {
        case Phase::Idle:
          break;
        case Phase::Delay:
          // `r.due_us` is read as an argument before begin_pulse overwrites
          // it, so the pulse is measured from the delay's deadline rather than
          // from the scan that happened to notice it.
          if (reached(r.due_us, now_us)) merge(out.ops, begin_pulse(i, now_us, r.due_us));
          break;
        case Phase::High:
          if (reached(r.due_us, now_us)) merge(out.ops, end_pulse(i, now_us));
          break;
        case Phase::Gap:
          if (reached(r.due_us, now_us)) merge(out.ops, begin_pulse(i, now_us, r.due_us));
          break;
      }
    }
  }

  // Pass two: the triggers, every one of them against the same word. Order
  // independent by construction, which is what lets a set renumber its timers
  // without changing what it does.
  //
  // Skipped whole when the word has not moved. `holds()` is a pure function of
  // the word, so an unchanged word means every predicate has the value it had
  // last scan and no false->true edge exists to find -- and the word includes
  // the timer bits, so a timer that started or ended in pass one has already
  // changed it. dev/PLAN.md's second rule, and the reason predicate scanning is
  // affordable at all.
  const LineBitmask word = world_word | bits_;
  if (!have_word_ || word != last_word_) {
    for (uint8_t i = 0; i < n; ++i) {
      const GlobalTimer& t = set_->timers[i];
      if (!has_trigger(t)) continue;
      const bool now_true = trigger_holds(t, word);
      // The predicate's false->true edge, exactly as a Transition fires -- and
      // like a Transition, never on the first word, because there is no
      // previous one for it to have been an edge against.
      if (have_word_ && now_true && !rt_[i].trigger_was_true) {
        merge(out.ops, start(i, now_us));
      }
      rt_[i].trigger_was_true = now_true;
    }
    // After the starts above, not before: one of them may have raised a bit,
    // and recording the pre-start word would make the next scan think the
    // world had moved when only this pass had.
    last_word_ = world_word | bits_;
    have_word_ = true;
  }

  out.bits = bits_;
  return out;
}

}  // namespace statemachined
