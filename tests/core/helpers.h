// A tiny builder, so a test reads as the paradigm it describes rather than as
// struct initialisation.
#pragma once
#include "engine.h"
#include "graph.h"

namespace fsmd::test {

struct Builder {
  StateGraph g;

  uint8_t fixed(int32_t ms) {
    g.dists[g.n_dists] = Distribution{DistributionKind::Fixed, 0, ms, 0, 0, nullptr, nullptr};
    return g.n_dists++;
  }
  uint8_t uniform(int32_t lo, int32_t hi) {
    g.dists[g.n_dists] =
        Distribution{DistributionKind::Uniform, 0, lo, hi, 0, nullptr, nullptr};
    return g.n_dists++;
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
    g.states[s].timeout_dist = dist;
    g.states[s].timeout_goto = target;
  }
  /// Conditions must be contiguous per state, so add them in one run per state.
  uint8_t on(uint8_t s, Condition c) {
    if (g.states[s].cond_count == 0) g.states[s].cond_first = g.n_conditions;
    g.conditions[g.n_conditions] = c;
    g.states[s].cond_count++;
    return g.n_conditions++;
  }
  void on_entry(uint8_t s, Action a) {
    if (g.states[s].entry_count == 0) g.states[s].entry_first = g.n_actions;
    g.actions[g.n_actions++] = a;
    g.states[s].entry_count++;
  }
  void on_exit(uint8_t s, Action a) {
    if (g.states[s].exit_count == 0) g.states[s].exit_first = g.n_actions;
    g.actions[g.n_actions++] = a;
    g.states[s].exit_count++;
  }
};

constexpr uint32_t bit(uint8_t n) { return 1u << n; }
constexpr uint32_t ms(uint32_t n) { return n * 1000u; }

}  // namespace fsmd::test
