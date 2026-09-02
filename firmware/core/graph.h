// The graph: states, their conditions, their output actions, and the outcomes
// terminal states name.
//
// Sparse per-state lists, not Bpod's dense [state][event] matrix. Bpod's gives
// O(1) lookup and is why it needs a Due or a Teensy; theirs notes that the
// feature profiles cost sRAM "in ways that are non-linear". Ours fits the Uno
// R4 Minima in ~7.5 KB, at the cost of scanning predicates -- bounded by
// evaluating only the *current* state's conditions and by skipping evaluation
// entirely when the input word has not changed.
//
// See dev/PLAN.md, "Fitting on 32 KB".
#pragma once
#include <cstdint>

#include "condition.h"
#include "config.h"
#include "rng.h"

namespace fsmd {

/// triald's .tdr outcome codes. A wire contract: these values are in every .tdr
/// the lab has written and every analysis script that reads one. NEVER
/// renumber. Mirrors triald's TrialOutcome and VStim's TDR::TrialOutcome.
enum class Outcome : int8_t {
  kUndetermined = -1,
  kNotStarted = 0,
  kHit = 1,
  kWrongResponse = 2,
  kEarlyHit = 3,
  kEarlyWrongResponse = 4,
  kEarly = 5,
  kLate = 6,
  kEyeError = 7,
  kInexpectedStartSignal = 8,
  kWrongStartSignal = 9,
  kCancelled = 10,
};

enum class ActionKind : uint8_t { kHigh = 0, kLow = 1, kToggle = 2, kPulse = 3 };

struct Action {
  uint8_t line = 0;
  ActionKind kind = ActionKind::kHigh;
  uint16_t ms = 0;  ///< kPulse only
};

struct State {
  uint8_t cond_first = 0, cond_count = 0;
  uint8_t entry_first = 0, entry_count = 0;
  uint8_t exit_first = 0, exit_count = 0;
  uint8_t timeout_dist = 0xFF;  ///< 0xFF = no timeout
  uint8_t timeout_goto = kNoState;
  Outcome outcome = Outcome::kUndetermined;  ///< set => terminal

  constexpr bool terminal() const { return outcome != Outcome::kUndetermined; }
};

/// Per-line input conditioning, applied when the word is assembled so that every
/// predicate above sees clean, polarity-normalised bits and no condition logic
/// has a special case. `invert` is Bpod's logicHigh/logicLow: opto-isolated
/// inputs are routinely active-low.
struct InputConfig {
  uint32_t invert_mask = 0;
  uint32_t enable_mask = 0xFFFFFFFF;
  uint16_t debounce_ms[kMaxLines] = {0};
};

struct Graph {
  uint16_t version = 0;
  uint8_t entry = kNoState;
  uint8_t n_states = 0;
  uint8_t n_conditions = 0;
  uint8_t n_actions = 0;
  uint8_t n_dists = 0;

  State states[kMaxStates];
  Condition conditions[kMaxConditions];
  Action actions[kMaxActions];
  Dist dists[kMaxDists];
  InputConfig inputs;

  /// Levels outputs are driven to on watchdog timeout, reset, link loss or a
  /// refused graph. Per line, because "off" is not always "low".
  uint32_t output_safe_levels = 0;
};

enum class GraphError : uint8_t {
  kNone = 0,
  kTooManyStates,
  kTooManyConditions,
  kTooManyActions,
  kTooManyDists,
  kBadEntry,
  kBadTarget,   ///< a transition to a state that does not exist
  kNoTerminal,  ///< no terminal state reachable from the entry state
  kUnreachableState,
};

/// Refuse a bad graph at upload, never at trial 300 -- triald "refuses rather
/// than failing later". Reachability analysis does not remove the need for the
/// runtime trial cap: static analysis cannot distinguish a 10 s foreperiod from
/// a hang.
GraphError validate(const Graph& g);

const char* graph_error_str(GraphError e);

}  // namespace fsmd
