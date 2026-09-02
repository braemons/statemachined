// The trial layer: a StateMachine plus the things that make a run a trial.
//
// This is the add-on, and the dependency runs one way only. The machine below
// has no notion of a trial id, an outcome or a cancel reason; this names all
// three. Splitting them is what lets the machine be tested on its own, and what
// would let a non-trial use of the same graph engine exist without dragging
// triald's outcome taxonomy along.
#pragma once
#include <cstdint>

#include "state_machine.h"
#include "trial.h"

namespace fsmd {

class TrialRunner {
 public:
  void set_graph(const StateGraph* g) { machine_.set_graph(g); }
  const StateGraph* graph() const { return machine_.graph(); }

  /// Begin a trial. The per-trial stream is derived rather than free-running:
  /// replaying trial 412 alone must draw trial 412's numbers, and a link reset
  /// must desynchronise nothing.
  OutputUpdate start(uint32_t trial_id, uint64_t session_seed, Microseconds now_us,
                     LineBitmask word = 0);

  OutputUpdate scan(LineBitmask word, Microseconds now_us);

  /// Cancel the trial. Returns false if it had already ended: the FIRST terminal
  /// decision wins, so a cancel arriving after a Hit reports the Hit rather than
  /// a fabricated Cancelled.
  bool cancel(TrialCancelReason why, Microseconds now_us);

  bool running() const { return machine_.is_running(); }
  StateIndex current_state() const { return machine_.get_current_state_index(); }

  const TrialRecord& result() const { return result_; }
  const StateMachineRunRecord& run() const { return machine_.get_record(); }

  void set_trial_cap_ms(Milliseconds ms) { machine_.set_run_cap_ms(ms); }

 private:
  /// Give the machine's terminal code, or its halt, a trial meaning. Runs after
  /// every scan; the first verdict sticks.
  void settle();

  StateMachine machine_;
  TrialRecord result_;
};

}  // namespace fsmd
