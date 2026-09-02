// The graph: states, their transitions, their output actions, and the outcomes
// terminal states name.
//
// Sparse per-state lists, not Bpod's dense [state][event] matrix. Bpod's gives
// O(1) lookup and is why it needs a Due or a Teensy; theirs notes that the
// feature profiles cost sRAM "in ways that are non-linear". Ours fits the Uno
// R4 Minima in ~7.5 KB, at the cost of scanning predicates -- bounded by
// evaluating only the *current* state's transitions and by skipping evaluation
// entirely when the input word has not changed.
//
// See dev/PLAN.md, "Fitting on 32 KB".
#pragma once
#include <cstdint>

#include "config.h"
#include "rng.h"
#include "transition.h"

namespace fsmd {

/// triald's .tdr outcome codes. A wire contract: these values are in every .tdr
/// the lab has written and every analysis script that reads one. NEVER
/// renumber. Mirrors triald's TrialOutcome and VStim's TDR::TrialOutcome.
enum class Outcome : int8_t {
  Undetermined = -1,
  NotStarted = 0,
  Hit = 1,
  WrongResponse = 2,
  EarlyHit = 3,
  EarlyWrongResponse = 4,
  Early = 5,
  Late = 6,
  EyeError = 7,
  InexpectedStartSignal = 8,
  WrongStartSignal = 9,
  Cancelled = 10,
};

enum class ActionKind : uint8_t { High = 0, Low = 1, Toggle = 2, Pulse = 3 };

struct Action {
  uint8_t line = 0;
  ActionKind kind = ActionKind::High;
  NarrowMilliseconds pulse_ms = 0;  ///< Pulse only
};

struct State {
  uint8_t trans_first = 0, trans_count = 0;
  uint8_t entry_first = 0, entry_count = 0;
  uint8_t exit_first = 0, exit_count = 0;
  DistributionIndex timeout_duration = kNoDistribution;
  uint8_t timeout_target = kNoState;
  Outcome outcome = Outcome::Undetermined;  ///< set => terminal

  constexpr bool terminal() const { return outcome != Outcome::Undetermined; }
};

/// Per-line input conditioning, applied when the word is assembled so that every
/// predicate above sees clean, polarity-normalised bits and no transition logic
/// has a special case. `invert` is Bpod's logicHigh/logicLow: opto-isolated
/// inputs are routinely active-low.
struct InputConfig {
  LineBitmask invert_mask = 0;
  LineBitmask enable_mask = 0xFFFFFFFF;
  NarrowMilliseconds debounce_ms[kMaxLines] = {0};
};

struct StateGraph {
  uint16_t version = 0;
  uint8_t entry = kNoState;
  uint8_t n_states = 0;
  uint8_t n_transitions = 0;
  uint8_t n_actions = 0;
  uint8_t n_distributions = 0;

  State states[kMaxStates];
  Transition transitions[kMaxTransitions];
  Action actions[kMaxActions];
  Distribution distributions[kMaxDistributions];
  InputConfig inputs;

  /// Levels outputs are driven to on watchdog timeout, reset, link loss or a
  /// refused graph. Per line, because "off" is not always "low".
  LineBitmask output_safe_levels = 0;
};

enum class GraphError : uint8_t {
  None = 0,
  TooManyStates,
  TooManyTransitions,
  TooManyActions,
  TooManyDistributions,
  BadEntry,
  BadTarget,      ///< a transition to a state that does not exist
  BadOutputLine,  ///< an action on a line the board cannot represent
  NoTerminal,     ///< no terminal state reachable from the entry state
  UnreachableState,
};

/// Refuse a bad graph at upload, never at trial 300 -- triald "refuses rather
/// than failing later". Reachability analysis does not remove the need for the
/// runtime trial cap: static analysis cannot distinguish a 10 s foreperiod from
/// a hang.
GraphError validate(const StateGraph& g);

const char* graph_error_str(GraphError e);

}  // namespace fsmd
