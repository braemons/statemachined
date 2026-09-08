// SPDX-License-Identifier: GPL-3.0-or-later
// Validation exists so a bad graph is refused at upload, never at trial 300 --
// triald "refuses rather than failing later". These tests are that promise.
#include "doctest.h"
#include "graph/graph_set.h"
#include "helpers.h"

using namespace statemachined;
using namespace statemachined::test;

TEST_CASE("a minimal well-formed graph validates") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.timeout(wait, b.fixed(100), hit);
  b.entry(wait);
  CHECK(validate(b.g) == GraphError::None);
}

TEST_CASE("an empty set is refused") {
  GraphSet g;
  CHECK(validate(g) == GraphError::TooManyGraphs);
}

TEST_CASE("a set whose graph has no states is refused") {
  GraphSet g;
  g.n_graphs = 1;
  CHECK(validate(g) == GraphError::TooManyStates);
}

TEST_CASE("two graphs share one set of pools") {
  // The whole mechanism: a graph is a (first_state, n_states) slice of the same
  // arrays a single graph used, one level up from how a State addresses its
  // transitions. Twenty independent graphs would be 52 KB on a 32 KB part.
  Builder b;
  const uint8_t a_wait = b.state();
  const uint8_t a_hit = b.terminal(TrialOutcome::Hit);
  b.timeout(a_wait, b.fixed(100), a_hit);
  b.entry(a_wait);

  b.graph();
  const uint8_t c_wait = b.state();
  const uint8_t c_hit = b.terminal(TrialOutcome::Hit);
  b.timeout(c_wait, b.fixed(200), c_hit);
  b.entry(c_wait);

  CHECK(validate(b.g) == GraphError::None);
  CHECK(b.g.n_graphs == 2);
  CHECK(b.g.n_states == 4);  // one pool, not two
  CHECK(b.g.graphs[0].first_state == 0);
  CHECK(b.g.graphs[1].first_state == 2);
  CHECK(b.g.graphs[1].entry == c_wait);
}

TEST_CASE("a transition may not leave its own graph") {
  // Selecting a graph by index has to select a *machine*. A set that let one
  // paradigm reach into another would make the index meaningless, and it is the
  // kind of mistake a shared pool makes easy: the target is a legal state.
  Builder b;
  const uint8_t a_wait = b.state();
  const uint8_t a_hit = b.terminal(TrialOutcome::Hit);
  b.timeout(a_wait, b.fixed(100), a_hit);
  b.entry(a_wait);

  b.graph();
  const uint8_t c_wait = b.state();
  b.terminal(TrialOutcome::Hit);
  b.timeout(c_wait, b.fixed(200), a_hit);  // into graph 0
  b.entry(c_wait);

  CHECK(validate(b.g) == GraphError::BadTarget);
}

TEST_CASE("every graph in the set is validated, not only the first") {
  // A set that uploaded four graphs and got three validated is a session that
  // fails at trial 40 rather than before the animal is in the booth.
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.timeout(wait, b.fixed(100), hit);
  b.entry(wait);

  b.graph();
  const uint8_t lonely = b.state();  // no terminal state reachable
  b.entry(lonely);

  CHECK(validate(b.g) == GraphError::NoTerminal);
}

TEST_CASE("a graph reaches only its own states when checking reachability") {
  // The counterpart of the transition rule: an unreachable state in graph 1
  // must not be excused by graph 0 being able to see it, and graph 0's states
  // must not be reported unreachable because graph 1 cannot reach them.
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.timeout(wait, b.fixed(100), hit);
  b.entry(wait);

  b.graph();
  const uint8_t c_wait = b.state();
  const uint8_t c_hit = b.terminal(TrialOutcome::Hit);
  b.state();  // an orphan in graph 1
  b.timeout(c_wait, b.fixed(200), c_hit);
  b.entry(c_wait);

  CHECK(validate(b.g) == GraphError::UnreachableState);
}

TEST_CASE("an entry state that does not exist is refused") {
  Builder b;
  b.terminal(TrialOutcome::Hit);
  b.entry(7);
  CHECK(validate(b.g) == GraphError::BadEntry);
}

TEST_CASE("a transition to a state that does not exist is refused") {
  Builder b;
  const uint8_t wait = b.state();
  b.terminal(TrialOutcome::Hit);
  b.timeout(wait, b.fixed(100), 42);
  b.entry(wait);
  CHECK(validate(b.g) == GraphError::BadTarget);

  Builder c;
  const uint8_t w = c.state();
  const uint8_t h = c.terminal(TrialOutcome::Hit);
  c.timeout(w, c.fixed(10), h);
  Transition cond;
  cond.all_high = bit(0);
  cond.target_state = 99;
  c.on(w, cond);
  c.entry(w);
  CHECK(validate(c.g) == GraphError::BadTarget);
}

TEST_CASE("a graph with no reachable terminal state is refused") {
  // This is the graph that would hang with a valve open. Refusing it at upload
  // is cheap; discovering it at trial 300 is not.
  Builder b;
  const uint8_t a = b.state();
  const uint8_t c = b.state();
  b.timeout(a, b.fixed(10), c);
  b.timeout(c, b.fixed(10), a);
  b.entry(a);
  CHECK(validate(b.g) == GraphError::NoTerminal);
}

TEST_CASE("a terminal state that exists but cannot be reached is refused") {
  Builder b;
  const uint8_t a = b.state();
  const uint8_t loop = b.state();
  b.terminal(TrialOutcome::Hit);  // reachable from nothing
  b.timeout(a, b.fixed(10), loop);
  b.timeout(loop, b.fixed(10), a);
  b.entry(a);
  const GraphError e = validate(b.g);
  CHECK((e == GraphError::NoTerminal || e == GraphError::UnreachableState));
}

TEST_CASE("an unreachable state is refused even when the graph can end") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.terminal(TrialOutcome::Late);  // nothing points here
  b.timeout(wait, b.fixed(100), hit);
  b.entry(wait);
  CHECK(validate(b.g) == GraphError::UnreachableState);
}

TEST_CASE("a state with no timeout and no transitions is a dead end") {
  Builder b;
  const uint8_t stuck = b.state();
  b.entry(stuck);
  CHECK(validate(b.g) == GraphError::NoTerminal);
}

TEST_CASE("an output action on a line the board does not have is refused") {
  // Regression: validate() bounded every list length but never the line number
  // inside an action, and apply_actions() shifts by it. A shift past the width
  // of a LineBitmask is undefined behaviour, and in practice it wraps -- line 40
  // silently drove line 8. On a rig line 8 is somebody's valve.
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.timeout(wait, b.fixed(10), hit);
  b.entry(wait);
  REQUIRE(validate(b.g) == GraphError::None);

  b.on_entry(wait, OutputAction{kMaxOutputLines, OutputActionKind::High, 0});
  CHECK(validate(b.g) == GraphError::BadOutputLine);

  b.g.output_actions[b.g.n_output_actions - 1].output_line = 40;
  CHECK(validate(b.g) == GraphError::BadOutputLine);

  // The last representable line is still fine.
  b.g.output_actions[b.g.n_output_actions - 1].output_line = kMaxOutputLines - 1;
  CHECK(validate(b.g) == GraphError::None);
}

TEST_CASE("a choice distribution with nothing to choose from is refused") {
  // draw() returns 0 for an empty Choice rather than reading past the array --
  // safe, and silently wrong. A graph asking for a random foreperiod would get
  // no foreperiod at all, every trial, and nothing downstream would say so.
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  const uint8_t d = b.fixed(10);
  b.timeout(wait, d, hit);
  b.entry(wait);
  REQUIRE(validate(b.g) == GraphError::None);

  b.g.distributions[d].kind = RandomDistributionKind::Choice;
  CHECK(validate(b.g) == GraphError::BadDistribution);

  static const Milliseconds opts[] = {100, 200, 300};
  b.g.distributions[d].opts = opts;
  CHECK(validate(b.g) == GraphError::BadDistribution);  // options, but n is 0

  b.g.distributions[d].n = 3;
  CHECK(validate(b.g) == GraphError::None);

  b.g.distributions[d].n = kMaxChoiceOptions + 1;
  CHECK(validate(b.g) == GraphError::BadDistribution);
}

TEST_CASE("every error has a message") {
  const GraphError every_error[] = {GraphError::None,
                                    GraphError::TooManyStates,
                                    GraphError::TooManyTransitions,
                                    GraphError::TooManyOutputActions,
                                    GraphError::TooManyDistributions,
                                    GraphError::BadEntry,
                                    GraphError::BadTarget,
                                    GraphError::BadOutputLine,
                                    GraphError::BadDistribution,
                                    GraphError::NoTerminal,
                                    GraphError::UnreachableState};
  for (GraphError e : every_error) {
    const char* m = graph_error_str(e);
    REQUIRE(m != nullptr);
    CHECK(m[0] != '\0');
  }
}

TEST_CASE("outcome codes are the .tdr wire contract") {
  // These values are in every .tdr the lab has written and every analysis
  // script that reads one. If this test fails, someone renumbered them.
  CHECK(static_cast<int>(TrialOutcome::Undetermined) == -1);
  CHECK(static_cast<int>(TrialOutcome::NotStarted) == 0);
  CHECK(static_cast<int>(TrialOutcome::Hit) == 1);
  CHECK(static_cast<int>(TrialOutcome::WrongResponse) == 2);
  CHECK(static_cast<int>(TrialOutcome::EarlyHit) == 3);
  CHECK(static_cast<int>(TrialOutcome::EarlyWrongResponse) == 4);
  CHECK(static_cast<int>(TrialOutcome::Early) == 5);
  CHECK(static_cast<int>(TrialOutcome::Late) == 6);
  CHECK(static_cast<int>(TrialOutcome::EyeError) == 7);
  CHECK(static_cast<int>(TrialOutcome::UnexpectedStartSignal) == 8);
  // The host's own, which this device can never produce and a graph may not
  // declare. Pinned anyway: the .tdr code space is one space, and the next
  // outcome added must not reuse 11.
  CHECK(static_cast<int>(TrialOutcome::NeverFinished) == 11);
  CHECK(static_cast<int>(TrialOutcome::WrongStartSignal) == 9);
  CHECK(static_cast<int>(TrialOutcome::Cancelled) == 10);
}

TEST_CASE("a state whose ranges run past the pool is refused") {
  // Everything in a graph is a (first, count) slice of one flat array, so a
  // slice that runs off the end is an out-of-bounds read at scan time -- inside
  // an ISR, on a board with no MMU. These are the bounds that make the runtime
  // memory-safe, so they are checked rather than assumed.
  const auto well_formed = [] {
    Builder b;
    const uint8_t wait = b.state();
    const uint8_t hit = b.terminal(TrialOutcome::Hit);
    b.timeout(wait, b.fixed(10), hit);
    b.on_entry(wait, OutputAction{0, OutputActionKind::High, 0});
    b.on_exit(wait, OutputAction{1, OutputActionKind::Low, 0});
    Transition t;
    t.all_high = bit(0);
    t.target_state = hit;
    b.on(wait, t);
    b.entry(wait);
    return b;
  };
  REQUIRE(validate(well_formed().g) == GraphError::None);

  SUBCASE("transitions") {
    Builder b = well_formed();
    b.g.states[0].transition_count = b.g.n_transitions + 1;
    CHECK(validate(b.g) == GraphError::TooManyTransitions);

    Builder c = well_formed();
    c.g.states[0].first_transition = c.g.n_transitions;
    c.g.states[0].transition_count = 1;
    CHECK(validate(c.g) == GraphError::TooManyTransitions);
  }

  SUBCASE("entry actions") {
    Builder b = well_formed();
    b.g.states[0].entry_action_count = b.g.n_output_actions + 1;
    CHECK(validate(b.g) == GraphError::TooManyOutputActions);
  }

  SUBCASE("exit actions") {
    Builder b = well_formed();
    b.g.states[0].first_exit_action = b.g.n_output_actions;
    b.g.states[0].exit_action_count = 1;
    CHECK(validate(b.g) == GraphError::TooManyOutputActions);
  }
}

TEST_CASE("a duration naming a distribution that does not exist is refused") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.timeout(wait, b.fixed(10), hit);
  Transition t;
  t.all_high = bit(0);
  t.target_state = hit;
  t.hold_duration = b.g.n_distributions;  // one past the pool
  b.on(wait, t);
  b.entry(wait);
  CHECK(validate(b.g) == GraphError::TooManyDistributions);

  // kNoRandomDistribution is the sentinel for "no hold", not an index, so it
  // must pass the same check that rejects a real out-of-range index.
  b.g.transitions[0].hold_duration = kNoRandomDistribution;
  CHECK(validate(b.g) == GraphError::None);

  b.g.states[wait].timeout_duration = b.g.n_distributions;
  CHECK(validate(b.g) == GraphError::TooManyDistributions);
}
