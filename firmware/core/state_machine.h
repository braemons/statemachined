// The scan loop: transitions, timers, output actions, and the record of a run.
//
// Deterministic in (graph, seed, input word, time): given the same sequence of
// calls it produces the same outputs and the same record on every board and on
// the host. Not pure -- it carries the run across scans, which is the point --
// but it reads no clock, touches no pin and allocates nothing, and no float
// reaches any value that crosses the wire. That is what makes the native build a
// real test of the firmware rather than a parallel implementation of it.
//
// It knows nothing about trials. Terminal states report an opaque TerminalCode;
// trial ids, outcomes and cancel reasons live a layer up in trial_runner.h, so
// this machine can be exercised, and reasoned about, on its own.
//
// The state is private on purpose. `raised_` is what guarantees every line a
// state raised is lowered by the same exit path, whatever the exit cause, so a
// valve cannot be left open by a graph that forgot something.
#pragma once
#include <cstdint>

#include "config.h"
#include "output_action.h"
#include "rng.h"
#include "state.h"
#include "state_graph.h"

namespace fsmd {

/// One visited state. `state_index`, not a name: the bridge holds the graph and
/// resolves names host-side, which is part of what keeps the device inside
/// 32 KB.
struct StateVisit {
  StateIndex state_index = 0;
  StateExitCause cause = StateExitCause::Terminal;
  TransitionIndex transition_index = kNoTransition;  ///< which transition fired,
                                                     ///< if StateExitCause::Transition
  Milliseconds drawn_ms = 0;  ///< the realised duration, reported so that a
                              ///< random draw is evidence and not just
                              ///< reproducible
  Microseconds entered_us = 0;
  Microseconds duration_us = 0;
};

/// What the machine saw during one run, with no interpretation attached.
struct StateMachineRunRecord {
  StateVisit path[kMaxPath];
  uint8_t path_len = 0;
  bool path_truncated = false;
  Microseconds total_us = 0;
  TerminalCode terminal_code = kNotTerminal;  ///< set if a terminal state was reached
  bool halted = false;                        ///< stopped by halt() instead
  bool hit_run_cap = false;                   ///< ...and specifically by the cap
};

class StateMachine {
 public:
  void set_graph(const StateGraph* g) { graph_ = g; }
  const StateGraph* graph() const { return graph_; }

  /// Begin a run. `now_us` is the arming instant. Returns the entry state's
  /// output actions -- they are outputs like any other and must not wait for
  /// the first scan.
  OutputUpdate start(uint64_t seed, Microseconds now_us, LineBitmask word = 0);

  /// One scan. `word` is the conditioned input word (debounced, polarity
  /// normalised, disabled lines zeroed) -- the HAL's job, not the machine's.
  OutputUpdate scan(LineBitmask word, Microseconds now_us);

  /// Force the run to end through the ordinary exit path, so every output the
  /// current state raised is lowered by the same code that lowers it on any
  /// other transition. Returns false if the run had already ended: the FIRST
  /// terminal decision wins, and we report what actually happened rather than a
  /// fabricated one.
  bool halt(Microseconds now_us);

  bool is_running() const { return running_; }
  StateIndex get_current_state_index() const { return current_; }
  const StateMachineRunRecord& get_record() const { return record_; }

  /// Wall-clock cap on a whole run. A graph is user data and may contain a
  /// state that never exits; validation cannot tell a 10 s foreperiod from a
  /// hang, so the cap stays regardless.
  void set_run_cap_ms(Milliseconds ms) { run_cap_ms_ = ms; }

 private:
  void enter(StateIndex state, Microseconds now_us, LineBitmask word);
  OutputUpdate leave(StateExitCause cause, TransitionIndex fired, Microseconds now_us);
  void record_visit(StateExitCause cause, TransitionIndex fired, Microseconds now_us);
  OutputUpdate apply_actions(OutputActionIndex first, uint8_t count) const;

  const StateGraph* graph_ = nullptr;
  Rng rng_;
  StateMachineRunRecord record_;
  TransitionState trans_state_[kMaxTransitions];

  bool running_ = false;
  StateIndex current_ = kNoState;
  Microseconds entered_us_ = 0;
  Microseconds started_us_ = 0;
  Milliseconds timeout_ms_ = -1;
  LineBitmask raised_ = 0;  ///< lines this state raised, lowered on exit
  LineBitmask last_word_ = 0;
  bool have_last_word_ = false;
  Milliseconds run_cap_ms_ = 0;
  OutputUpdate pending_;       ///< outputs owed by a halt that arrived between
  bool has_pending_ = false;   ///< scans; applied on the next one
  bool hold_pending_ = false;  ///< a transition is accumulating a hold, so an
                               ///< unchanged input word still needs work
};

}  // namespace fsmd
