#include "graph.h"

namespace fsmd {

GraphError validate(const Graph& g) {
  if (g.n_states == 0 || g.n_states > kMaxStates) return GraphError::kTooManyStates;
  if (g.n_conditions > kMaxConditions) return GraphError::kTooManyConditions;
  if (g.n_actions > kMaxActions) return GraphError::kTooManyActions;
  if (g.n_dists > kMaxDists) return GraphError::kTooManyDists;
  if (g.entry >= g.n_states) return GraphError::kBadEntry;

  for (uint8_t i = 0; i < g.n_states; ++i) {
    const State& s = g.states[i];
    if (s.cond_first + s.cond_count > g.n_conditions) return GraphError::kTooManyConditions;
    if (s.entry_first + s.entry_count > g.n_actions) return GraphError::kTooManyActions;
    if (s.exit_first + s.exit_count > g.n_actions) return GraphError::kTooManyActions;
    if (s.timeout_dist != 0xFF) {
      if (s.timeout_dist >= g.n_dists) return GraphError::kTooManyDists;
      if (s.timeout_goto >= g.n_states) return GraphError::kBadTarget;
    }
    for (uint8_t c = 0; c < s.cond_count; ++c) {
      const Condition& cond = g.conditions[s.cond_first + c];
      if (cond.goto_state >= g.n_states) return GraphError::kBadTarget;
      if (cond.hold_dist != 0xFF && cond.hold_dist >= g.n_dists)
        return GraphError::kTooManyDists;
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

  if (!terminal_reachable) return GraphError::kNoTerminal;
  for (uint8_t i = 0; i < g.n_states; ++i)
    if (!seen[i]) return GraphError::kUnreachableState;

  return GraphError::kNone;
}

const char* graph_error_str(GraphError e) {
  switch (e) {
    case GraphError::kNone:
      return "ok";
    case GraphError::kTooManyStates:
      return "too many states";
    case GraphError::kTooManyConditions:
      return "too many conditions";
    case GraphError::kTooManyActions:
      return "too many output actions";
    case GraphError::kTooManyDists:
      return "too many distributions";
    case GraphError::kBadEntry:
      return "entry state does not exist";
    case GraphError::kBadTarget:
      return "transition to a state that does not exist";
    case GraphError::kNoTerminal:
      return "no terminal state is reachable from the entry state";
    case GraphError::kUnreachableState:
      return "a state is unreachable from the entry state";
  }
  return "unknown";
}

}  // namespace fsmd
