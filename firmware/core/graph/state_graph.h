// The graph: states, their transitions, their output actions, and the codes
// terminal states report.
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
#include "graph/output_action.h"
#include "graph/state.h"
#include "graph/transition.h"
#include "random/random_distribution.h"

namespace fsmd {

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
  StateIndex entry = kNoState;
  uint8_t n_states = 0;
  uint8_t n_transitions = 0;
  uint8_t n_output_actions = 0;
  uint8_t n_distributions = 0;

  uint8_t n_choice_options = 0;

  State states[kMaxStates];
  Transition transitions[kMaxTransitions];
  OutputAction output_actions[kMaxOutputActions];
  RandomDistribution distributions[kMaxDistributions];

  /// Backing store for every Choice distribution's options, shared the way the
  /// other pools are. A RandomDistribution's `opts` and `weights` point in
  /// here for an uploaded graph; a hand-built one may point anywhere, so
  /// validate() checks that they exist rather than where they live.
  Milliseconds choice_options[kMaxChoiceOptions] = {0};
  uint16_t choice_weights[kMaxChoiceOptions] = {0};

  InputConfig inputs;

  /// Levels outputs are driven to on watchdog timeout, reset, link loss or a
  /// refused graph. Per line, because "off" is not always "low".
  LineBitmask output_safe_levels = 0;

  StateGraph() = default;

  /// Copying re-points every Choice distribution that pointed into the source's
  /// own option pool, so the copy refers to its own.
  ///
  /// Without this a memberwise copy leaves the new graph's distributions
  /// pointing at the old graph's arrays -- which works, silently, until the old
  /// graph is overwritten by the next upload, and then a foreperiod is drawn
  /// from whatever a later paradigm happened to leave there. A distribution
  /// pointing at a static array (which is how the tests build one) is left
  /// alone, since it was never pointing into a pool to begin with.
  StateGraph(const StateGraph& other) { assign(other); }
  StateGraph& operator=(const StateGraph& other) {
    if (this != &other) assign(other);
    return *this;
  }

 private:
  void assign(const StateGraph& other);
};

enum class GraphError : uint8_t {
  None = 0,
  TooManyStates,
  TooManyTransitions,
  TooManyOutputActions,
  TooManyDistributions,
  BadEntry,
  BadTarget,        ///< a transition to a state that does not exist
  BadOutputLine,    ///< an action on a line the board cannot represent
  BadDistribution,  ///< a Choice with no options to choose from
  BadPulse,         ///< a Pulse with no width, which would never come down
  NoTerminal,       ///< no terminal state reachable from the entry state
  UnreachableState,
};

/// Refuse a bad graph at upload, never at trial 300 -- triald "refuses rather
/// than failing later". Reachability analysis does not remove the need for the
/// runtime trial cap: static analysis cannot distinguish a 10 s foreperiod from
/// a hang.
GraphError validate(const StateGraph& g);

const char* graph_error_str(GraphError e);

}  // namespace fsmd
