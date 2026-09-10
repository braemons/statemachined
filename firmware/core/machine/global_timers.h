// SPDX-License-Identifier: GPL-3.0-or-later
// The global timers, ticking beside the state machine.
//
// **Why this is not inside StateMachine.** A timer has to survive the end of a
// run -- that is most of what makes it global -- and the machine does not: the
// session builds a fresh TrialRunner, and so a fresh StateMachine, for every
// trial (`runner_ = TrialRunner(live_set_, graph_index)`). A timer living in
// there would be silently reset at every trial boundary, which is the one thing
// it must not do. So the bank is owned by the session, ticked by the same scan,
// and hands the machine its bits as ordinary input lines. StateMachine is
// untouched and still knows nothing about trials -- or about timers.
//
// **What it is.** Eight timers, each holding `timer_line(i)` of the input word
// high while it runs, each optionally driving one real output line alongside.
// The order within a scan is fixed and matters:
//
//   1. Advance every timer's own phase against the clock. A timer whose width
//      has elapsed drops its bit here.
//   2. Evaluate every trigger against the word *including* the bits step 1 just
//      settled, and start whatever fires.
//
// Two passes rather than one so that triggering is order-independent: every
// timer sees the same word, so a timer chained to another one starts on the
// same scan its source ended rather than on the scan after, and renumbering the
// timers cannot change what a set does. VStim ticks its queue in index order
// within one vertical retrace, which is the same idea with the order left in.
//
// See graph/global_timer.h for the model and for what it borrows from where.
#pragma once
#include <cstdint>

#include "config.h"
#include "graph/graph_set.h"
#include "graph/output_action.h"
#include "random/rng.h"

namespace statemachined {

/// What one tick of the bank produced.
struct TimerTick {
  /// Real output lines to move, for the HAL. Empty unless a timer that names an
  /// `output_line` started or ended this scan.
  OutputUpdate ops;
  /// Every timer line high after this tick, to be OR'd into the input word
  /// before the machine sees it.
  LineBitmask bits = 0;
};

class GlobalTimerBank {
 public:
  /// Which timers may run at all, one bit per timer index.
  ///
  /// VStim's `m_TimerActive` and `m_StimList` between them: a timer is switched
  /// on for the rig, and separately declares which trials it takes part in.
  /// Here that is one mask, set at device scope by the `timers` command and
  /// optionally overridden for a single trial by `configure` -- the same shape
  /// as a distribution `patch`, and for the same reason. It is what lets a host
  /// map a trial type onto a set of timers the way `graph_index` maps one onto
  /// a graph.
  ///
  /// Disabling a timer that is running stops it and puts its line back, rather
  /// than letting it finish: a mask that took effect only at the next trigger
  /// would leave a line up that nothing in the new configuration accounts for.
  OutputUpdate set_enabled(uint32_t mask, Microseconds now_us);
  uint32_t enabled() const { return enabled_; }

  /// Point the bank at a set and clear every timer. Called when a set is
  /// committed, and on any reset that abandons what the timers were doing.
  ///
  /// Returns the lines to move: a timer that was running has to have its output
  /// put back, and a set upload is exactly when nothing else will do it.
  OutputUpdate bind(const GraphSet* set, Microseconds now_us);

  /// Its own stream, derived from the session seed but separate from every
  /// trial's, so that declaring a timer does not shift a single number the
  /// graph draws. Replay of a trial has to be unaffected by whether the set
  /// around it happens to run timers.
  void reseed(uint64_t session_seed);

  /// One scan. `world_word` is the conditioned input word -- real lines only;
  /// the bank ORs its own bits in itself.
  TimerTick tick(LineBitmask world_word, Microseconds now_us);

  /// Start timer `i` now, as a TimerStart output action does. Ignored if it is
  /// already running: VStim's DelayPulse looks at its trigger only in
  /// WaitTrigger, so a re-trigger mid-pulse does not restart it, and a state
  /// re-entered in a loop must not keep pushing the same timer's end away.
  OutputUpdate start(uint8_t i, Microseconds now_us);

  /// Stop timer `i` now, dropping its bit and putting its output line back.
  OutputUpdate cancel(uint8_t i, Microseconds now_us);

  /// A run has ended. Resets the timers that declared `trial_bound` and leaves
  /// the rest running -- VStim's `ResetTimer()`, which is per timer and not a
  /// property of the device.
  OutputUpdate end_of_run(Microseconds now_us);

  /// Every timer line currently high.
  LineBitmask bits() const { return bits_; }

  /// Whether any timer is running at all, so a caller can skip the whole thing.
  bool any_running() const { return bits_ != 0 || armed_ != 0; }

 private:
  /// Where one timer is in its own cycle. VStim's STATES, minus the two that
  /// only its frequency divider uses.
  enum class Phase : uint8_t {
    Idle = 0,   ///< not running; waiting for a trigger
    Delay = 1,  ///< triggered, counting down the onset delay
    High = 2,   ///< the bit, and the line, are up
    Gap = 3,    ///< between two pulses of a loop
  };

  struct Runtime {
    Phase phase = Phase::Idle;
    Microseconds due_us = 0;        ///< when the current phase ends
    uint8_t loops_left = 0;         ///< pulses still owed; 0 with phase != Idle means forever
    bool forever = false;           ///< `loops` was 0
    bool trigger_was_true = false;  ///< the edge latch, as a Transition has
  };

  /// `from_us` is the deadline this phase is measured from -- the previous
  /// phase's, never the scan that noticed it. See next_deadline().
  OutputUpdate begin_pulse(uint8_t i, Microseconds now_us, Microseconds from_us);
  OutputUpdate end_pulse(uint8_t i, Microseconds now_us);
  OutputUpdate drive(uint8_t i, bool on);
  Milliseconds draw(RandomDistributionIndex d);

  const GraphSet* set_ = nullptr;
  Rng rng_;
  Runtime rt_[kMaxTimers];
  LineBitmask bits_ = 0;   ///< timer lines high
  LineBitmask armed_ = 0;  ///< timers not Idle, including those still in Delay
  /// Every timer enabled. All of them until something says otherwise, so a set
  /// that declares timers and a host that never mentions them behaves the way
  /// the set reads.
  uint32_t enabled_ = 0xFFFFFFFFu;
  /// The word the triggers were last evaluated against, timer bits included.
  ///
  /// `holds()` is a pure function of the word, so an unchanged word cannot
  /// produce a false->true edge on any timer and the whole trigger pass can be
  /// skipped. That is dev/PLAN.md's second rule -- the one that bounds the cost
  /// of scanning predicates rather than indexing a matrix -- and the same
  /// reason StateMachine::advance keeps `last_word_`. On a rig at rest it turns
  /// eight predicate evaluations per scan into one compare.
  LineBitmask last_word_ = 0;
  /// Until the first tick there is no previous word, so no trigger can have an
  /// edge yet. Without this a predicate already true when a set is committed
  /// would fire every timer on the first scan.
  bool have_word_ = false;
};

}  // namespace statemachined
