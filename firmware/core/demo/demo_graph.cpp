// SPDX-License-Identifier: GPL-3.0-or-later
#include "demo/demo_graph.h"

#include "graph/output_action.h"
#include "graph/state.h"
#include "graph/transition.h"
#include "trial/trial.h"

namespace statemachined {
namespace demo {
namespace {

constexpr LineBitmask bit(LineIndex n) { return static_cast<LineBitmask>(1u) << n; }

/// The pools are filled by hand rather than through the tests' Builder, which
/// lives under tests/ and never ships. The one rule that is easy to get wrong:
/// a state's transitions and its actions are (first, count) *slices*, so each
/// state's entries have to be added contiguously.
struct Fill {
  StateGraph& g;

  uint8_t fixed_ms(Milliseconds ms) {
    RandomDistribution& d = g.distributions[g.n_distributions];
    d = RandomDistribution{};
    d.kind = RandomDistributionKind::Fixed;
    d.a = ms;
    return g.n_distributions++;
  }

  uint8_t state() {
    g.states[g.n_states] = State{};
    return g.n_states++;
  }

  uint8_t terminal(TrialOutcome o) {
    const uint8_t s = state();
    g.states[s].terminal_code = terminal_code_of(o);
    return s;
  }

  void timeout(uint8_t s, uint8_t dist, uint8_t target) {
    g.states[s].timeout_duration = dist;
    g.states[s].timeout_target = target;
  }

  void on_rise(uint8_t s, LineIndex line, uint8_t target) {
    if (g.states[s].transition_count == 0) g.states[s].first_transition = g.n_transitions;
    Transition& t = g.transitions[g.n_transitions];
    t = Transition{};
    t.all_high = bit(line);
    t.target_state = target;
    // The default: fire on the predicate's rising edge, so a switch already
    // held down when the state is entered does not skip straight through it.
    t.fire_if_true_on_entry = false;
    g.states[s].transition_count++;
    ++g.n_transitions;
  }

  void raise_on_entry(uint8_t s, LineIndex line) {
    if (g.states[s].entry_action_count == 0)
      g.states[s].first_entry_action = g.n_output_actions;
    OutputAction& a = g.output_actions[g.n_output_actions];
    a = OutputAction{};
    a.output_line = line;
    a.kind = OutputActionKind::High;
    g.states[s].entry_action_count++;
    ++g.n_output_actions;
  }
};

}  // namespace

void build(StateGraph& g) {
  g = StateGraph{};
  Fill f{g};

  const uint8_t step = f.fixed_ms(kStepMs);

  // States are allocated before their transitions are filled in, because a
  // transition names its target and the chase points forward.
  const uint8_t wait = f.state();
  uint8_t steps[kStepCount];
  for (uint8_t i = 0; i < kStepCount; ++i) steps[i] = f.state();
  const uint8_t done = f.terminal(TrialOutcome::Hit);
  const uint8_t aborted = f.terminal(TrialOutcome::Cancelled);

  g.entry = wait;
  g.version = 1;

  // Sitting idle, with the ready lamp on, until the switch is pressed. No
  // timeout: the board should wait all afternoon rather than start on its own.
  f.raise_on_entry(wait, kReadyOutput);
  f.on_rise(wait, kStartInput, steps[0]);

  for (uint8_t i = 0; i < kStepCount; ++i) {
    f.raise_on_entry(steps[i], static_cast<LineIndex>(kFirstStepOutput + i));
    // The abort switch is checked in every step, which is the cheap way to see
    // that a transition beats a timeout that has not expired yet.
    f.on_rise(steps[i], kAbortInput, aborted);
    f.timeout(steps[i], step, i + 1 < kStepCount ? steps[i + 1] : done);
  }

  // Both terminal states light a lamp that stays on, since nothing exits a
  // terminal state. Reusing the step LEDs would be ambiguous, so the outcome
  // gets its own: the ready lamp for a completed trial, and the first step LED
  // is not it.
  f.raise_on_entry(done, kReadyOutput);
  f.raise_on_entry(aborted, static_cast<LineIndex>(kFirstStepOutput));

  // Debounce every input the demo uses. A bench switch bounces for a few
  // milliseconds and an undebounced one would fire the chase several times.
  g.inputs.debounce_ms[kStartInput] = 20;
  g.inputs.debounce_ms[kAbortInput] = 20;
  g.output_safe_levels = 0;
}

}  // namespace demo
}  // namespace statemachined
