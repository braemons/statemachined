// The scan loop: conditions, timers, transitions, and the trial record.
//
// A pure function of (graph, seed, input word, time). No I/O, no Arduino.h, no
// allocation, no float on any path whose result crosses the wire. That is what
// makes the native build a real test of the firmware rather than a parallel
// implementation of it -- the same lesson as triald's note that the debug
// controls are not a second code path.
#pragma once
#include <cstdint>

#include "graph.h"

namespace fsmd {

enum class ExitCause : uint8_t {
  kTimeout = 0,
  kCondition = 1,
  kCancel = 2,
  kTerminal = 3,  ///< the trial ended here
};

enum class CancelReason : uint8_t {
  kNone = 0,
  kHost = 1,          ///< the experimenter, via triald and the bridge
  kLinkLost = 2,      ///< heartbeat gap
  kAbortLine = 3,     ///< the always-live hardware abort input
  kTrialTimeout = 4,  ///< the wall-clock cap on total trial duration
};

/// One visited state. `state_index`, not a name: the bridge holds the graph and
/// resolves names host-side, which is part of what keeps the device inside
/// 32 KB.
struct PathEntry {
  uint8_t state_index = 0;
  ExitCause cause = ExitCause::kTerminal;
  uint8_t condition_index = 0xFF;  ///< which condition fired, if kCondition
  int32_t drawn_ms = 0;            ///< the realised duration, reported so that
                                   ///< a random draw is evidence and not just
                                   ///< reproducible
  uint32_t entered_us = 0;
  uint32_t duration_us = 0;
};

struct Result {
  uint32_t trial_id = 0;
  Outcome outcome = Outcome::kUndetermined;
  CancelReason cancel_reason = CancelReason::kNone;
  PathEntry path[kMaxPath];
  uint8_t path_len = 0;
  bool path_truncated = false;
  uint32_t total_us = 0;
};

/// What the engine wants done to the outputs this scan. The HAL applies it; the
/// engine never touches a pin, which is what lets the whole thing run on the
/// host.
struct OutputOps {
  uint32_t set_high = 0;
  uint32_t set_low = 0;
};

class Engine {
 public:
  void set_graph(const Graph* g) { graph_ = g; }
  const Graph* graph() const { return graph_; }

  /// Begin a trial. `now_us` is the arming instant. Returns the entry
  /// state's output actions -- they are outputs like any other and must not
  /// wait for the first scan.
  OutputOps start(uint32_t trial_id, uint64_t session_seed, uint32_t now_us, uint32_t word = 0);

  /// One scan. `word` is the conditioned input word (debounced, polarity
  /// normalised, disabled lines zeroed) -- the HAL's job, not the engine's.
  /// Returns the outputs to apply.
  OutputOps scan(uint32_t word, uint32_t now_us);

  /// Force the trial to a terminal state through the ordinary exit path, so
  /// every output the current state raised is lowered by the same code that
  /// lowers it on any other transition. Returns false if the trial had already
  /// ended: the FIRST terminal decision wins, and we report what actually
  /// happened rather than a fabricated CANCELLED.
  bool cancel(CancelReason why, uint32_t now_us);

  bool running() const { return running_; }
  uint8_t current_state() const { return current_; }
  const Result& result() const { return result_; }

  /// Wall-clock cap on a whole trial. A graph is user data and may contain a
  /// state that never exits; validation cannot tell a 10 s foreperiod from a
  /// hang, so the cap stays regardless.
  void set_trial_cap_ms(uint32_t ms) { trial_cap_ms_ = ms; }

 private:
  void enter(uint8_t state, uint32_t now_us, uint32_t word);
  OutputOps leave(ExitCause cause, uint8_t cond_index, uint32_t now_us);
  void record(ExitCause cause, uint8_t cond_index, uint32_t now_us);
  OutputOps apply_actions(uint8_t first, uint8_t count) const;

  const Graph* graph_ = nullptr;
  Rng rng_;
  Result result_;
  ConditionState cond_state_[kMaxConditions];

  bool running_ = false;
  uint8_t current_ = kNoState;
  uint32_t entered_us_ = 0;
  uint32_t started_us_ = 0;
  int32_t timeout_ms_ = -1;
  uint32_t raised_ = 0;  ///< lines this state raised, lowered on exit
  uint32_t last_word_ = 0;
  bool have_last_word_ = false;
  uint32_t trial_cap_ms_ = 0;
  OutputOps pending_;          ///< outputs owed by a cancel that arrived
  bool has_pending_ = false;   ///< between scans; applied on the next one
  bool hold_pending_ = false;  ///< a condition is accumulating a hold, so
                               ///< an unchanged input word still needs work
};

}  // namespace fsmd
