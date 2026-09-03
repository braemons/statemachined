// SPDX-License-Identifier: GPL-3.0-or-later
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
// valve cannot be left open by a graph that forgot something. The one place a
// line outlives its state is a terminal state's entry actions -- nothing can
// exit a terminal state, so those are carried in `raised_` and lowered by the
// next start().
//
// It also owns the two output actions that need more than a level: a Pulse's
// falling edge and a Toggle's direction. Both could have been left to the HAL,
// and both are here instead, because a pulse width decided per board is a pulse
// width nothing on the host can test and four HALs get subtly differently. The
// HAL is left with "drive these lines high, drive those low", which is the one
// thing it is actually better placed to do.
#pragma once
#include <cstdint>

#include "config.h"
#include "graph/output_action.h"
#include "graph/state.h"
#include "graph/state_graph.h"
#include "random/rng.h"

namespace statemachined {

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
  bool force_ended = false;                   ///< ended by force_end() rather than by the graph
  bool hit_run_cap = false;                   ///< ...and specifically by the cap
};

class StateMachine {
 public:
  /// The graph arrives at construction and never changes. A machine without one
  /// would be an object with no legal operation on it, and a set_graph() would
  /// mean every method had to consider the case where nobody had called it.
  /// Committing a different graph means constructing a different machine --
  /// which is also the honest semantics, since a new graph invalidates every
  /// index the old run was recording.
  explicit StateMachine(const StateGraph& g) : graph_(&g) {}

  const StateGraph& graph() const { return *graph_; }

  /// Begin a run. `now_us` is the arming instant. Returns the entry state's
  /// output actions -- they are outputs like any other and must not wait for
  /// the first scan.
  OutputUpdate start(uint64_t seed, Microseconds now_us, LineBitmask word = 0);

  /// Lines owed to the world irrespective of any run: a Pulse whose width has
  /// elapsed, and the outputs owed by a force_end() that landed between scans.
  ///
  /// Call it every scan, whether or not a trial is in flight. A pulse on a
  /// terminal state's entry -- the ordinary way to write a reward -- is raised
  /// by the last act of a run, so if this only ticked while running, the valve
  /// would stay open until the next trial started. advance() calls it first, so
  /// a running machine needs nothing extra.
  OutputUpdate service_outputs(Microseconds now_us);

  /// Tell the machine what the pins are already at, so the first Toggle goes
  /// the right way. The levels after a reset are the graph's safe levels, not
  /// zero, and the machine cannot read a pin to find out.
  void set_initial_levels(LineBitmask levels) { driven_ = levels; }

  /// What the machine believes the output lines are at. Its own shadow, built
  /// from what it has emitted -- if the HAL is driven by something else as
  /// well, this is the machine's view and not the board's.
  LineBitmask driven_levels() const { return driven_; }

  /// Move the run forward to `now_us`, given the input lines as they are at
  /// that instant. This is the whole scan loop and the only thing called
  /// periodically -- 10 kHz on the reference board.
  ///
  /// In order, it: pays out any outputs owed by a force_end() that landed
  /// between calls; ends the run if the wall-clock cap has expired; evaluates
  /// the *current* state's transitions against `word`, in declaration order,
  /// the first whose predicate holds winning; failing that, checks the state's
  /// timeout; and if either fired, leaves the state and enters the target,
  /// which may be terminal. At most one transition happens per call.
  ///
  /// Returns the lines to move as a result, and moves none itself -- the HAL
  /// drives them. A call in which nothing happened returns an empty update,
  /// which is the common case and costs one compare when the input word has
  /// not changed.
  ///
  /// `word` is the *conditioned* input word: debounced, polarity normalised,
  /// disabled lines zeroed. InputConditioner does all three -- the HAL reads a
  /// port register and nothing more.
  OutputUpdate advance(LineBitmask word, Microseconds now_us);

  /// End the run now, from outside the graph -- a cancel, a lost link, the
  /// wall-clock cap. Not "stop": the run is over afterwards and its record is
  /// final.
  ///
  /// It ends through the *ordinary* exit path, so every output line the current
  /// state raised is lowered by the same code that lowers it on any other
  /// transition. The valve closing on a cancel is not a special case somebody
  /// has to remember to write; it is the only path there is. The record gets a
  /// final StateVisit with StateExitCause::Cancel and `force_ended` set, and no
  /// terminal code -- the machine does not invent one.
  ///
  /// Returns false if the run had already ended, in which case nothing happens.
  /// The FIRST terminal decision wins: a cancel arriving after the graph
  /// reached a terminal state loses, and the real outcome is reported rather
  /// than a fabricated one.
  ///
  /// The outputs it owes are handed back by the next advance() rather than
  /// returned here, because a cancel typically arrives between scans and the
  /// HAL applies output updates where it applies every other one.
  bool force_end(Microseconds now_us);

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
  OutputUpdate apply_actions(OutputActionIndex first, uint8_t count, Microseconds now_us);

  /// Fold an update into the shadow. Idempotent over an update that has already
  /// been folded, so it can be applied again to a merged one.
  void note(const OutputUpdate& ops);

  const StateGraph* graph_;
  Rng rng_;
  StateMachineRunRecord record_;
  TransitionState trans_state_[kMaxTransitions];

  bool running_ = false;
  StateIndex current_ = kNoState;
  Microseconds entered_us_ = 0;
  Microseconds started_us_ = 0;
  Milliseconds timeout_ms_ = -1;
  LineBitmask raised_ = 0;  ///< lines this state raised, lowered on exit

  // Pulses. A deadline per line rather than a queue: a line can only be pulsing
  // once, and re-pulsing a line that is already pulsing means the new width,
  // which is what a graph re-entering a state means by it. 128 B on a 32-line
  // board, and the hot path is one compare against `pulsing_` when nothing is.
  LineBitmask pulsing_ = 0;
  Microseconds pulse_deadline_us_[kMaxOutputLines] = {0};

  /// The machine's shadow of the output levels. Needed for Toggle, which has no
  /// meaning without knowing where the line is now.
  LineBitmask driven_ = 0;

  LineBitmask last_word_ = 0;
  bool have_last_word_ = false;
  Milliseconds run_cap_ms_ = 0;
  OutputUpdate pending_;       ///< outputs owed by a halt that arrived between
  bool has_pending_ = false;   ///< scans; applied on the next one
  bool hold_pending_ = false;  ///< a transition is accumulating a hold, so an
                               ///< unchanged input word still needs work
};

}  // namespace statemachined
