// SPDX-License-Identifier: GPL-3.0-or-later
// The graphs a session uses: states, their transitions, their output actions,
// and the codes terminal states report -- all of them, in one set of pools.
//
// A *set* rather than a graph, since M4c. A session declares every graph it
// will use, they are uploaded once before the first trial, and `configure`
// picks one by index. That is what keeps an upload out of the inter-trial
// interval, and out of the inter-trial interval of only those trials where the
// type changed -- which would be a timing difference correlated with the
// variable under study. See docs/developer/daemon.md 3.2.
//
// They share one set of pools, which is the mechanism this file already used
// one level down: a State addresses its transitions and actions as a
// (first, count) slice. A graph is the same thing one level up, and costs three
// bytes. Twenty independent StateGraphs would have been 52 KB on a 32 KB part.
//
// Sparse per-state lists, not Bpod's dense [state][event] matrix. Bpod's gives
// O(1) lookup and is why it needs a Due or a Teensy; theirs notes that the
// feature profiles cost sRAM "in ways that are non-linear". Ours fits the Uno
// R4 Minima in ~7.5 KB, at the cost of scanning predicates -- bounded by
// evaluating only the *current* state's transitions and by skipping evaluation
// entirely when the input word has not changed.
//
// The wiring -- invert, enable, debounce, the output safe levels -- is
// deliberately NOT in here. It describes the box, not the paradigm, and it
// lives in io/wiring.h at device scope; see the comment there for why that is a
// fail-safe fix rather than tidiness.
//
// See dev/PLAN.md, "Fitting on 32 KB".
#pragma once
#include <cstdint>

#include "config.h"
#include "graph/output_action.h"
#include "graph/state.h"
#include "graph/transition.h"
#include "random/random_distribution.h"

namespace statemachined {

/// One graph inside the set: where it starts, and which slice of the shared
/// state pool is its own.
///
/// Three bytes, so twenty of them is sixty. Indices on the *wire* are per-graph
/// -- a host that authored a four-state paradigm counts its states from zero --
/// and the device adds `first_state` on the way in. Everything below this
/// struct therefore works in absolute pool indices and never has to know a set
/// exists.
struct GraphEntry {
  StateIndex entry = kNoState;  ///< absolute index into GraphSet::states
  StateIndex first_state = 0;   ///< absolute; the slice this graph owns
  uint8_t n_states = 0;
};

struct GraphSet {
  /// The host's identifier for the whole set. `configure` carries it, so a set
  /// edit that did not land cannot leave the device confidently running the
  /// old paradigms.
  uint16_t version = 0;

  uint8_t n_graphs = 0;
  GraphEntry graphs[kMaxGraphs];

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

  GraphSet() = default;

  /// The graph at `i`, unchecked. Callers hold an index the builder or the
  /// session already validated.
  const GraphEntry& graph(uint8_t i) const { return graphs[i]; }

  /// Copying re-points every Choice distribution that pointed into the source's
  /// own option pool, so the copy refers to its own.
  ///
  /// Without this a memberwise copy leaves the new graph's distributions
  /// pointing at the old graph's arrays -- which works, silently, until the old
  /// graph is overwritten by the next upload, and then a foreperiod is drawn
  /// from whatever a later paradigm happened to leave there. A distribution
  /// pointing at a static array (which is how the tests build one) is left
  /// alone, since it was never pointing into a pool to begin with.
  GraphSet(const GraphSet& other) { assign(other); }
  GraphSet& operator=(const GraphSet& other) {
    if (this != &other) assign(other);
    return *this;
  }

 private:
  void assign(const GraphSet& other);
};

enum class GraphError : uint8_t {
  None = 0,
  TooManyStates,
  TooManyTransitions,
  TooManyOutputActions,
  TooManyDistributions,
  BadEntry,
  BadTarget,  ///< a transition to a state that does not exist, or to one
              ///< belonging to a different graph in the set
  TooManyGraphs,
  EmptyGraph,          ///< a slot in the set that no graph_begin filled
  RelightOnLiveState,  ///< a dwell on a state that is not terminal, which is
                       ///< drawn on the end of a run and so could never be read
  BadOutputLine,       ///< an action on a line the board cannot represent
  BadDistribution,     ///< a Choice with no options to choose from
  BadPulse,            ///< a Pulse with no width, which would never come down
  NoTerminal,          ///< no terminal state reachable from the entry state
  UnreachableState,
};

/// Refuse a bad graph at upload, never at trial 300 -- triald "refuses rather
/// than failing later". Reachability analysis does not remove the need for the
/// runtime trial cap: static analysis cannot distinguish a 10 s foreperiod from
/// a hang.
/// Every graph in the set, and the pools they share. A set is committed only if
/// all of it passes: a session that uploaded four graphs and got three is a
/// session that fails at trial 40 instead of before the animal is in the booth.
GraphError validate(const GraphSet& s);

const char* graph_error_str(GraphError e);

}  // namespace statemachined
