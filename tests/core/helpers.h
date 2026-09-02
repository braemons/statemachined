// A tiny builder, so a test reads as the paradigm it describes rather than as
// struct initialisation.
#pragma once
#include "engine.h"
#include "graph.h"

namespace fsmd::test {

struct Builder {
  StateGraph g;

  uint8_t fixed(int32_t ms) {
    g.distributions[g.n_distributions] =
        Distribution{DistributionKind::Fixed, 0, ms, 0, 0, nullptr, nullptr};
    return g.n_distributions++;
  }
  uint8_t uniform(int32_t lo, int32_t hi) {
    g.distributions[g.n_distributions] =
        Distribution{DistributionKind::Uniform, 0, lo, hi, 0, nullptr, nullptr};
    return g.n_distributions++;
  }

  uint8_t state() {
    g.states[g.n_states] = State{};
    return g.n_states++;
  }
  uint8_t terminal(Outcome o) {
    const uint8_t s = state();
    g.states[s].outcome = o;
    return s;
  }
  void timeout(uint8_t s, uint8_t dist, uint8_t target) {
    g.states[s].timeout_duration = dist;
    g.states[s].timeout_target = target;
  }
  /// Transitions must be contiguous per state, so add them in one run per state.
  uint8_t on(uint8_t s, Transition c) {
    if (g.states[s].trans_count == 0) g.states[s].trans_first = g.n_transitions;
    g.transitions[g.n_transitions] = c;
    g.states[s].trans_count++;
    return g.n_transitions++;
  }
  void on_entry(uint8_t s, OutputAction a) {
    if (g.states[s].entry_count == 0) g.states[s].entry_first = g.n_output_actions;
    g.output_actions[g.n_output_actions++] = a;
    g.states[s].entry_count++;
  }
  void on_exit(uint8_t s, OutputAction a) {
    if (g.states[s].exit_count == 0) g.states[s].exit_first = g.n_output_actions;
    g.output_actions[g.n_output_actions++] = a;
    g.states[s].exit_count++;
  }
};

constexpr uint32_t bit(uint8_t n) { return 1u << n; }
constexpr uint32_t ms(uint32_t n) { return n * 1000u; }

}  // namespace fsmd::test
