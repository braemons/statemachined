// Validation exists so a bad graph is refused at upload, never at trial 300 --
// triald "refuses rather than failing later". These tests are that promise.
#include "doctest.h"
#include "graph.h"
#include "helpers.h"

using namespace fsmd;
using namespace fsmd::test;

TEST_CASE("a minimal well-formed graph validates") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(Outcome::Hit);
  b.timeout(wait, b.fixed(100), hit);
  b.g.entry = wait;
  CHECK(validate(b.g) == GraphError::None);
}

TEST_CASE("an empty graph is refused") {
  StateGraph g;
  CHECK(validate(g) == GraphError::TooManyStates);
}

TEST_CASE("an entry state that does not exist is refused") {
  Builder b;
  b.terminal(Outcome::Hit);
  b.g.entry = 7;
  CHECK(validate(b.g) == GraphError::BadEntry);
}

TEST_CASE("a transition to a state that does not exist is refused") {
  Builder b;
  const uint8_t wait = b.state();
  b.terminal(Outcome::Hit);
  b.timeout(wait, b.fixed(100), 42);
  b.g.entry = wait;
  CHECK(validate(b.g) == GraphError::BadTarget);

  Builder c;
  const uint8_t w = c.state();
  const uint8_t h = c.terminal(Outcome::Hit);
  c.timeout(w, c.fixed(10), h);
  Transition cond;
  cond.all_high = bit(0);
  cond.target_state = 99;
  c.on(w, cond);
  c.g.entry = w;
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
  b.g.entry = a;
  CHECK(validate(b.g) == GraphError::NoTerminal);
}

TEST_CASE("a terminal state that exists but cannot be reached is refused") {
  Builder b;
  const uint8_t a = b.state();
  const uint8_t loop = b.state();
  b.terminal(Outcome::Hit);  // reachable from nothing
  b.timeout(a, b.fixed(10), loop);
  b.timeout(loop, b.fixed(10), a);
  b.g.entry = a;
  const GraphError e = validate(b.g);
  CHECK((e == GraphError::NoTerminal || e == GraphError::UnreachableState));
}

TEST_CASE("an unreachable state is refused even when the graph can end") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(Outcome::Hit);
  b.terminal(Outcome::Late);  // nothing points here
  b.timeout(wait, b.fixed(100), hit);
  b.g.entry = wait;
  CHECK(validate(b.g) == GraphError::UnreachableState);
}

TEST_CASE("a state with no timeout and no transitions is a dead end") {
  Builder b;
  const uint8_t stuck = b.state();
  b.g.entry = stuck;
  CHECK(validate(b.g) == GraphError::NoTerminal);
}

TEST_CASE("every error has a message") {
  const GraphError every_error[] = {GraphError::None,
                                    GraphError::TooManyStates,
                                    GraphError::TooManyTransitions,
                                    GraphError::TooManyActions,
                                    GraphError::TooManyDistributions,
                                    GraphError::BadEntry,
                                    GraphError::BadTarget,
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
  CHECK(static_cast<int>(Outcome::Undetermined) == -1);
  CHECK(static_cast<int>(Outcome::NotStarted) == 0);
  CHECK(static_cast<int>(Outcome::Hit) == 1);
  CHECK(static_cast<int>(Outcome::WrongResponse) == 2);
  CHECK(static_cast<int>(Outcome::EarlyHit) == 3);
  CHECK(static_cast<int>(Outcome::EarlyWrongResponse) == 4);
  CHECK(static_cast<int>(Outcome::Early) == 5);
  CHECK(static_cast<int>(Outcome::Late) == 6);
  CHECK(static_cast<int>(Outcome::EyeError) == 7);
  CHECK(static_cast<int>(Outcome::InexpectedStartSignal) == 8);
  CHECK(static_cast<int>(Outcome::WrongStartSignal) == 9);
  CHECK(static_cast<int>(Outcome::Cancelled) == 10);
}
