// SPDX-License-Identifier: GPL-3.0-or-later
// A tiny builder, so a test reads as the paradigm it describes rather than as
// struct initialisation.
#pragma once
#include "graph/graph_set.h"
#include "trial/trial.h"
#include "trial/trial_runner.h"

namespace statemachined::test {

struct Builder {
  GraphSet g;
  /// Which graph in the set is being filled. A test that says nothing about
  /// sets builds a set of one, which is what almost all of them want.
  uint8_t current = 0;

  Builder() {
    g.n_graphs = 1;
    g.graphs[0].first_state = 0;
  }

  /// Start another graph in the same set. Its states carry on in the shared
  /// pool -- which is the whole mechanism -- so `state()` keeps returning
  /// absolute indices and a test names targets the way it always did.
  uint8_t graph() {
    const uint8_t i = g.n_graphs++;
    g.graphs[i].first_state = g.n_states;
    g.graphs[i].n_states = 0;
    current = i;
    return i;
  }

  void entry(uint8_t s) { g.graphs[current].entry = s; }

  uint8_t fixed(int32_t ms) {
    g.distributions[g.n_distributions] =
        RandomDistribution{RandomDistributionKind::Fixed, 0, ms, 0, 0, nullptr, nullptr};
    return g.n_distributions++;
  }
  uint8_t uniform(int32_t lo, int32_t hi) {
    g.distributions[g.n_distributions] =
        RandomDistribution{RandomDistributionKind::Uniform, 0, lo, hi, 0, nullptr, nullptr};
    return g.n_distributions++;
  }

  uint8_t state() {
    g.states[g.n_states] = State{};
    ++g.graphs[current].n_states;
    return g.n_states++;
  }
  /// A terminal state reporting a raw code -- for tests that exercise the state
  /// machine without any trial vocabulary.
  uint8_t terminal_code(TerminalCode c) {
    const uint8_t s = state();
    g.states[s].terminal_code = c;
    return s;
  }
  uint8_t terminal(TrialOutcome o) { return terminal_code(terminal_code_of(o)); }
  void timeout(uint8_t s, uint8_t dist, uint8_t target) {
    g.states[s].timeout_duration = dist;
    g.states[s].timeout_target = target;
  }
  /// Transitions must be contiguous per state, so add them in one run per state.
  uint8_t on(uint8_t s, Transition c) {
    if (g.states[s].transition_count == 0) g.states[s].first_transition = g.n_transitions;
    g.transitions[g.n_transitions] = c;
    g.states[s].transition_count++;
    return g.n_transitions++;
  }
  void on_entry(uint8_t s, OutputAction a) {
    if (g.states[s].entry_action_count == 0)
      g.states[s].first_entry_action = g.n_output_actions;
    g.output_actions[g.n_output_actions++] = a;
    g.states[s].entry_action_count++;
  }
  void on_exit(uint8_t s, OutputAction a) {
    if (g.states[s].exit_action_count == 0) g.states[s].first_exit_action = g.n_output_actions;
    g.output_actions[g.n_output_actions++] = a;
    g.states[s].exit_action_count++;
  }
};

constexpr uint32_t bit(uint8_t n) { return 1u << n; }
constexpr uint32_t ms(uint32_t n) { return n * 1000u; }

}  // namespace statemachined::test
