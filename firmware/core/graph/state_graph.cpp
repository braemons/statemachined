// SPDX-License-Identifier: GPL-3.0-or-later
#include "graph/state_graph.h"

#include <cstddef>
#include <cstdint>

namespace statemachined {
namespace {

/// Where `p` sits in `g`'s option pool, or -1 if it is not in it at all. The
/// comparison goes through uintptr_t rather than the pointers themselves:
/// relational comparison of pointers into different objects is not something
/// the standard defines, and this is asked exactly about pointers that may be
/// into a different object.
ptrdiff_t pool_offset(const StateGraph& g, const Milliseconds* p) {
  if (p == nullptr) return -1;
  const uintptr_t base = reinterpret_cast<uintptr_t>(&g.choice_options[0]);
  const uintptr_t end = base + sizeof(g.choice_options);
  const uintptr_t at = reinterpret_cast<uintptr_t>(p);
  if (at < base || at >= end) return -1;
  return static_cast<ptrdiff_t>((at - base) / sizeof(Milliseconds));
}

ptrdiff_t weight_offset(const StateGraph& g, const uint16_t* p) {
  if (p == nullptr) return -1;
  const uintptr_t base = reinterpret_cast<uintptr_t>(&g.choice_weights[0]);
  const uintptr_t end = base + sizeof(g.choice_weights);
  const uintptr_t at = reinterpret_cast<uintptr_t>(p);
  if (at < base || at >= end) return -1;
  return static_cast<ptrdiff_t>((at - base) / sizeof(uint16_t));
}

}  // namespace

void StateGraph::assign(const StateGraph& other) {
  version = other.version;
  entry = other.entry;
  n_states = other.n_states;
  n_transitions = other.n_transitions;
  n_output_actions = other.n_output_actions;
  n_distributions = other.n_distributions;
  n_choice_options = other.n_choice_options;

  for (uint8_t i = 0; i < n_states; ++i) states[i] = other.states[i];
  for (uint8_t i = 0; i < n_transitions; ++i) transitions[i] = other.transitions[i];
  for (uint8_t i = 0; i < n_output_actions; ++i) output_actions[i] = other.output_actions[i];
  for (uint8_t i = 0; i < n_choice_options; ++i) {
    choice_options[i] = other.choice_options[i];
    choice_weights[i] = other.choice_weights[i];
  }

  // Re-point only what pointed into the source's own pool. A distribution
  // aimed at a static array -- how a hand-built graph does it -- never referred
  // to a pool and must be left exactly as it was.
  for (uint8_t i = 0; i < n_distributions; ++i) {
    distributions[i] = other.distributions[i];
    const ptrdiff_t o = pool_offset(other, other.distributions[i].opts);
    if (o >= 0) distributions[i].opts = &choice_options[o];
    const ptrdiff_t w = weight_offset(other, other.distributions[i].weights);
    if (w >= 0) distributions[i].weights = &choice_weights[w];
  }
}

GraphError validate(const StateGraph& g) {
  if (g.n_states == 0 || g.n_states > kMaxStates) return GraphError::TooManyStates;
  if (g.n_transitions > kMaxTransitions) return GraphError::TooManyTransitions;
  if (g.n_output_actions > kMaxOutputActions) return GraphError::TooManyOutputActions;
  if (g.n_distributions > kMaxDistributions) return GraphError::TooManyDistributions;
  if (g.entry >= g.n_states) return GraphError::BadEntry;

  // Every action in the pool, not just the reachable ones: apply_actions()
  // shifts by this line number, and a shift past the width of a LineBitmask is
  // undefined behaviour. In practice it wraps, so line 40 silently drives line
  // 8 -- a graph asking for a line that does not exist must be refused here,
  // not quietly redirected onto a valve at trial 300.
  for (uint8_t i = 0; i < g.n_output_actions; ++i) {
    const OutputAction& a = g.output_actions[i];
    if (a.output_line >= kMaxOutputLines) return GraphError::BadOutputLine;
    // A zero-width pulse raises a line and schedules its fall for the same
    // instant. Whether that reaches a pin at all depends on when the scan
    // lands, so it is a graph that means nothing in particular -- refuse it
    // rather than let a reward be silently zero.
    if (a.kind == OutputActionKind::Pulse && a.pulse_ms == 0) return GraphError::BadPulse;
  }

  // A Choice with no options draws from nothing. draw() returns 0 rather than
  // reading past an empty array, which is safe and silently wrong: a graph
  // asking for a random foreperiod would get no foreperiod at all, every trial,
  // and nothing downstream would say so.
  for (uint8_t i = 0; i < g.n_distributions; ++i) {
    const RandomDistribution& d = g.distributions[i];
    if (d.kind != RandomDistributionKind::Choice) continue;
    if (d.n == 0 || d.opts == nullptr) return GraphError::BadDistribution;
    if (d.n > kMaxChoiceOptions) return GraphError::BadDistribution;
  }

  for (uint8_t i = 0; i < g.n_states; ++i) {
    const State& s = g.states[i];
    if (s.first_transition + s.transition_count > g.n_transitions)
      return GraphError::TooManyTransitions;
    if (s.first_entry_action + s.entry_action_count > g.n_output_actions)
      return GraphError::TooManyOutputActions;
    if (s.first_exit_action + s.exit_action_count > g.n_output_actions)
      return GraphError::TooManyOutputActions;
    if (s.timeout_duration != kNoRandomDistribution) {
      if (s.timeout_duration >= g.n_distributions) return GraphError::TooManyDistributions;
      if (s.timeout_target >= g.n_states) return GraphError::BadTarget;
    }
    for (uint8_t c = 0; c < s.transition_count; ++c) {
      const Transition& t = g.transitions[s.first_transition + c];
      if (t.target_state >= g.n_states) return GraphError::BadTarget;
      if (t.hold_duration != kNoRandomDistribution && t.hold_duration >= g.n_distributions)
        return GraphError::TooManyDistributions;
    }
  }

  // Reachability from the entry state, and whether a terminal state is among
  // what is reachable. A graph that cannot end is a graph that hangs with
  // outputs high.
  bool seen[kMaxStates] = {false};
  uint8_t stack[kMaxStates];
  uint8_t top = 0;
  stack[top++] = g.entry;
  seen[g.entry] = true;
  bool terminal_reachable = false;

  while (top > 0) {
    const State& s = g.states[stack[--top]];
    if (s.terminal()) terminal_reachable = true;
    auto push = [&](uint8_t t) {
      if (t < g.n_states && !seen[t]) {
        seen[t] = true;
        stack[top++] = t;
      }
    };
    if (s.timeout_duration != kNoRandomDistribution) push(s.timeout_target);
    for (uint8_t c = 0; c < s.transition_count; ++c)
      push(g.transitions[s.first_transition + c].target_state);
  }

  if (!terminal_reachable) return GraphError::NoTerminal;
  for (uint8_t i = 0; i < g.n_states; ++i)
    if (!seen[i]) return GraphError::UnreachableState;

  return GraphError::None;
}

const char* graph_error_str(GraphError e) {
  switch (e) {
    case GraphError::None:
      return "ok";
    case GraphError::TooManyStates:
      return "too many states";
    case GraphError::TooManyTransitions:
      return "too many transitions";
    case GraphError::TooManyOutputActions:
      return "too many output output_actions";
    case GraphError::TooManyDistributions:
      return "too many distributions";
    case GraphError::BadEntry:
      return "entry state does not exist";
    case GraphError::BadTarget:
      return "transition to a state that does not exist";
    case GraphError::BadOutputLine:
      return "an output action names a line the board does not have";
    case GraphError::BadPulse:
      return "a pulse output action has no width, so it would never come down";
    case GraphError::BadDistribution:
      return "a choice distribution has no options to choose from";
    case GraphError::NoTerminal:
      return "no terminal state is reachable from the entry state";
    case GraphError::UnreachableState:
      return "a state is unreachable from the entry state";
  }
  return "unknown";
}

}  // namespace statemachined
