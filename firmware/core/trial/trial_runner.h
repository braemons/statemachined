// SPDX-License-Identifier: GPL-3.0-or-later
// The trial layer: a StateMachine plus the things that make a run a trial.
//
// This is the add-on, and the dependency runs one way only. The machine below
// has no notion of a trial id, an outcome or a cancel reason; this names all
// three. Splitting them is what lets the machine be tested on its own, and what
// would let a non-trial use of the same graph engine exist without dragging
// triald's outcome taxonomy along.
#pragma once
#include <cstdint>

#include "machine/state_machine.h"
#include "trial/trial.h"

namespace statemachined {

class TrialRunner {
 public:
  explicit TrialRunner(const StateGraph& g) : machine_(g) {}

  const StateGraph& graph() const { return machine_.graph(); }

  /// Begin a trial. The per-trial stream is derived rather than free-running:
  /// replaying trial 412 alone must draw trial 412's numbers, and a link reset
  /// must desynchronise nothing.
  OutputUpdate start(uint32_t trial_id, uint64_t session_seed, Microseconds now_us,
                     LineBitmask word = 0);

  /// The machine's advance(), plus latch_outcome() after it -- so the trial has
  /// a verdict the instant the run acquires one, rather than whenever somebody
  /// next thinks to ask.
  OutputUpdate advance(LineBitmask word, Microseconds now_us);

  /// Cancel the trial. Returns false if it had already ended: the FIRST terminal
  /// decision wins, so a cancel arriving after a Hit reports the Hit rather than
  /// a fabricated Cancelled.
  bool cancel(TrialCancelReason why, Microseconds now_us);

  /// Pulses and other debts, which are owed whether or not a trial is running.
  /// See StateMachine::service_outputs().
  OutputUpdate service_outputs(Microseconds now_us) { return machine_.service_outputs(now_us); }

  /// What the engine believes the output lines are at. See
  /// StateMachine::driven_levels().
  LineBitmask driven_levels() const { return machine_.driven_levels(); }

  /// Seed the machine's shadow of the output levels, so the first Toggle goes
  /// the right way. See StateMachine::set_initial_levels().
  void set_initial_levels(LineBitmask levels) { machine_.set_initial_levels(levels); }

  bool running() const { return machine_.is_running(); }
  StateIndex current_state() const { return machine_.get_current_state_index(); }

  const TrialRecord& result() const { return result_; }
  const StateMachineRunRecord& run_record() const { return machine_.get_record(); }

  void set_trial_cap_ms(Milliseconds ms) { machine_.set_run_cap_ms(ms); }

 private:
  /// Read the run's ending, if it has one, and write down what it meant for the
  /// trial -- terminal code to TrialOutcome, or the wall-clock cap to
  /// Cancelled/TrialTimeout, which only this layer knows is a cancellation
  /// rather than simply the end of a run.
  ///
  /// Latched, in the electronics sense: once the outcome is anything but
  /// Undetermined this returns immediately and the verdict never changes. That
  /// is what makes "the first terminal decision wins" true no matter how often
  /// it is called, and it is called after every advance().
  void latch_outcome();

  StateMachine machine_;
  TrialRecord result_;
};

}  // namespace statemachined
