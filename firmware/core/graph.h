// The graph: states, their transitions, their output output_actions, and the outcomes
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
enum class TrialOutcome : int8_t {
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

enum class OutputActionKind : uint8_t { High = 0, Low = 1, Toggle = 2, Pulse = 3 };

struct OutputAction {
  uint8_t output_line = 0;
  OutputActionKind kind = OutputActionKind::High;
  NarrowMilliseconds pulse_ms = 0;  ///< Pulse only
};

/// A node of the graph. Its transitions and its output actions live in the
/// graph's shared pools rather than inside the state, so each is a (first,
/// count) run into the relevant array -- a slice, not a list. That is what makes
/// a 32-state graph fit in ~7.5 KB where Bpod's dense [state][event] matrix
/// needs a Due or a Teensy.
struct State {
  TransitionIndex first_transition = 0;  ///< slice of StateGraph::transitions
  uint8_t transition_count = 0;

  OutputActionIndex first_entry_action = 0;  ///< slice of output_actions, raised
  uint8_t entry_action_count = 0;            ///< on entry

  OutputActionIndex first_exit_action = 0;  ///< slice of output_actions, applied
  uint8_t exit_action_count = 0;            ///< on exit, beyond the automatic
                                            ///< lowering of everything raised

  /// Drawn once on entry; kNoRandomDistribution means the state has no timeout
  /// and can only be left through a transition or a cancel.
  RandomDistributionIndex timeout_duration = kNoRandomDistribution;
  StateIndex timeout_target = kNoState;  ///< where a timeout goes

  TrialOutcome outcome = TrialOutcome::Undetermined;  ///< set => terminal

  constexpr bool terminal() const { return outcome != TrialOutcome::Undetermined; }
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
  uint8_t n_output_actions = 0;
  uint8_t n_distributions = 0;

  State states[kMaxStates];
  Transition transitions[kMaxTransitions];
  OutputAction output_actions[kMaxOutputActions];
  RandomDistribution distributions[kMaxDistributions];
  InputConfig inputs;

  /// Levels outputs are driven to on watchdog timeout, reset, link loss or a
  /// refused graph. Per line, because "off" is not always "low".
  LineBitmask output_safe_levels = 0;
};

enum class GraphError : uint8_t {
  None = 0,
  TooManyStates,
  TooManyTransitions,
  TooManyOutputActions,
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
