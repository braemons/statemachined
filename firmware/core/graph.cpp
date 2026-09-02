#include "graph.h"

namespace fsmd {

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
  for (uint8_t i = 0; i < g.n_output_actions; ++i)
    if (g.output_actions[i].output_line >= kMaxOutputLines) return GraphError::BadOutputLine;

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
      const Transition& cond = g.transitions[s.first_transition + c];
      if (cond.target_state >= g.n_states) return GraphError::BadTarget;
      if (cond.hold_duration != kNoRandomDistribution &&
          cond.hold_duration >= g.n_distributions)
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
    case GraphError::NoTerminal:
      return "no terminal state is reachable from the entry state";
    case GraphError::UnreachableState:
      return "a state is unreachable from the entry state";
  }
  return "unknown";
}

}  // namespace fsmd
