#include "graph.h"

namespace fsmd {

GraphError validate(const StateGraph& g) {
  if (g.n_states == 0 || g.n_states > kMaxStates) return GraphError::TooManyStates;
  if (g.n_conditions > kMaxConditions) return GraphError::TooManyConditions;
  if (g.n_actions > kMaxActions) return GraphError::TooManyActions;
  if (g.n_dists > kMaxDists) return GraphError::TooManyDists;
  if (g.entry >= g.n_states) return GraphError::BadEntry;

  for (uint8_t i = 0; i < g.n_states; ++i) {
    const State& s = g.states[i];
    if (s.cond_first + s.cond_count > g.n_conditions) return GraphError::TooManyConditions;
    if (s.entry_first + s.entry_count > g.n_actions) return GraphError::TooManyActions;
    if (s.exit_first + s.exit_count > g.n_actions) return GraphError::TooManyActions;
    if (s.timeout_dist != 0xFF) {
      if (s.timeout_dist >= g.n_dists) return GraphError::TooManyDists;
      if (s.timeout_goto >= g.n_states) return GraphError::BadTarget;
    }
    for (uint8_t c = 0; c < s.cond_count; ++c) {
      const Condition& cond = g.conditions[s.cond_first + c];
      if (cond.goto_state >= g.n_states) return GraphError::BadTarget;
      if (cond.hold_dist != 0xFF && cond.hold_dist >= g.n_dists)
        return GraphError::TooManyDists;
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
    if (s.timeout_dist != 0xFF) push(s.timeout_goto);
    for (uint8_t c = 0; c < s.cond_count; ++c) push(g.conditions[s.cond_first + c].goto_state);
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
    case GraphError::TooManyConditions:
      return "too many conditions";
    case GraphError::TooManyActions:
      return "too many output actions";
    case GraphError::TooManyDists:
      return "too many distributions";
    case GraphError::BadEntry:
      return "entry state does not exist";
    case GraphError::BadTarget:
      return "transition to a state that does not exist";
    case GraphError::NoTerminal:
      return "no terminal state is reachable from the entry state";
    case GraphError::UnreachableState:
      return "a state is unreachable from the entry state";
  }
  return "unknown";
}

}  // namespace fsmd
