// The scan loop: transitions, timers, transitions, and the trial record.
//
// Deterministic in (graph, seed, input word, time): given the same sequence of
// calls it produces the same outputs and the same record on every board and on
// the host. Not pure -- it carries the trial across scans, which is the point --
// but it reads no clock, touches no pin and allocates nothing, and no float
// reaches any value that crosses the wire. That is what makes the native build a
// real test of the firmware rather than a parallel implementation of it -- the
// same lesson as triald's note that the debug controls are not a second code
// path.
//
// The state is private on purpose. `raised_` is what guarantees every line a
// state raised is lowered by the same exit path, whatever the exit cause, so a
// valve cannot be left open by a graph that forgot something.
#pragma once
#include <cstdint>

#include "graph.h"

namespace fsmd {

enum class ExitCause : uint8_t {
  Timeout = 0,
  Transition = 1,
  Cancel = 2,
  Terminal = 3,  ///< the trial ended here
};

enum class CancelReason : uint8_t {
  None = 0,
  Host = 1,          ///< the experimenter, via triald and the bridge
  LinkLost = 2,      ///< heartbeat gap
  AbortLine = 3,     ///< the always-live hardware abort input
  TrialTimeout = 4,  ///< the wall-clock cap on total trial duration
};

/// One visited state. `state_index`, not a name: the bridge holds the graph and
/// resolves names host-side, which is part of what keeps the device inside
/// 32 KB.
struct StateVisit {
  uint8_t state_index = 0;
  ExitCause cause = ExitCause::Terminal;
  uint8_t transition_index =
      kNoTransition;          ///< which transition fired, if ExitCause::Transition
  Milliseconds drawn_ms = 0;  ///< the realised duration, reported so that
                              ///< a random draw is evidence and not just
                              ///< reproducible
  Microseconds entered_us = 0;
  Microseconds duration_us = 0;
};

struct TrialRecord {
  uint32_t trial_id = 0;
  Outcome outcome = Outcome::Undetermined;
  CancelReason cancel_reason = CancelReason::None;
  StateVisit path[kMaxPath];
  uint8_t path_len = 0;
  bool path_truncated = false;
  Microseconds total_us = 0;
};

/// What the engine wants done to the outputs this scan. The HAL applies it; the
/// engine never touches a pin, which is what lets the whole thing run on the
/// host.
struct OutputUpdate {
  LineBitmask set_high = 0;  ///< lines to drive high this scan
  LineBitmask set_low = 0;   ///< lines to drive low this scan
};

class TrialStateMachine {
 public:
  void set_graph(const StateGraph* g) { graph_ = g; }
  const StateGraph* graph() const { return graph_; }

  /// Begin a trial. `now_us` is the arming instant. Returns the entry
  /// state's output actions -- they are outputs like any other and must not
  /// wait for the first scan.
  OutputUpdate start(uint32_t trial_id, uint64_t session_seed, Microseconds now_us,
                     LineBitmask word = 0);

  /// One scan. `word` is the conditioned input word (debounced, polarity
  /// normalised, disabled lines zeroed) -- the HAL's job, not the engine's.
  /// Returns the outputs to apply.
  OutputUpdate scan(LineBitmask word, Microseconds now_us);

  /// Force the trial to a terminal state through the ordinary exit path, so
  /// every output the current state raised is lowered by the same code that
  /// lowers it on any other transition. Returns false if the trial had already
  /// ended: the FIRST terminal decision wins, and we report what actually
  /// happened rather than a fabricated CANCELLED.
  bool cancel(CancelReason why, Microseconds now_us);

  bool running() const { return running_; }
  uint8_t current_state() const { return current_; }
  const TrialRecord& result() const { return result_; }

  /// Wall-clock cap on a whole trial. A graph is user data and may contain a
  /// state that never exits; validation cannot tell a 10 s foreperiod from a
  /// hang, so the cap stays regardless.
  void set_trial_cap_ms(Milliseconds ms) { trial_cap_ms_ = ms; }

 private:
  void enter(uint8_t state, Microseconds now_us, LineBitmask word);
  OutputUpdate leave(ExitCause cause, uint8_t trans_index, Microseconds now_us);
  void record(ExitCause cause, uint8_t trans_index, Microseconds now_us);
  OutputUpdate apply_actions(uint8_t first, uint8_t count) const;

  const StateGraph* graph_ = nullptr;
  Rng rng_;
  TrialRecord result_;
  TransitionState trans_state_[kMaxTransitions];

  bool running_ = false;
  uint8_t current_ = kNoState;
  Microseconds entered_us_ = 0;
  Microseconds started_us_ = 0;
  Milliseconds timeout_ms_ = -1;
  LineBitmask raised_ = 0;  ///< lines this state raised, lowered on exit
  LineBitmask last_word_ = 0;
  bool have_last_word_ = false;
  Milliseconds trial_cap_ms_ = 0;
  OutputUpdate pending_;       ///< outputs owed by a cancel that arrived
  bool has_pending_ = false;   ///< between scans; applied on the next one
  bool hold_pending_ = false;  ///< a transition is accumulating a hold, so
                               ///< an unchanged input word still needs work
};

}  // namespace fsmd
