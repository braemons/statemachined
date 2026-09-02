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
  const uint8_t hit = b.terminal(Outcome::kHit);
  b.timeout(wait, b.fixed(100), hit);
  b.g.entry = wait;
  CHECK(validate(b.g) == GraphError::kNone);
}

TEST_CASE("an empty graph is refused") {
  Graph g;
  CHECK(validate(g) == GraphError::kTooManyStates);
}

TEST_CASE("an entry state that does not exist is refused") {
  Builder b;
  b.terminal(Outcome::kHit);
  b.g.entry = 7;
  CHECK(validate(b.g) == GraphError::kBadEntry);
}

TEST_CASE("a transition to a state that does not exist is refused") {
  Builder b;
  const uint8_t wait = b.state();
  b.terminal(Outcome::kHit);
  b.timeout(wait, b.fixed(100), 42);
  b.g.entry = wait;
  CHECK(validate(b.g) == GraphError::kBadTarget);

  Builder c;
  const uint8_t w = c.state();
  const uint8_t h = c.terminal(Outcome::kHit);
  c.timeout(w, c.fixed(10), h);
  Condition cond;
  cond.all = bit(0);
  cond.goto_state = 99;
  c.on(w, cond);
  c.g.entry = w;
  CHECK(validate(c.g) == GraphError::kBadTarget);
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
  CHECK(validate(b.g) == GraphError::kNoTerminal);
}

TEST_CASE("a terminal state that exists but cannot be reached is refused") {
  Builder b;
  const uint8_t a = b.state();
  const uint8_t loop = b.state();
  b.terminal(Outcome::kHit);  // reachable from nothing
  b.timeout(a, b.fixed(10), loop);
  b.timeout(loop, b.fixed(10), a);
  b.g.entry = a;
  const GraphError e = validate(b.g);
  CHECK((e == GraphError::kNoTerminal || e == GraphError::kUnreachableState));
}

TEST_CASE("an unreachable state is refused even when the graph can end") {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(Outcome::kHit);
  b.terminal(Outcome::kLate);  // nothing points here
  b.timeout(wait, b.fixed(100), hit);
  b.g.entry = wait;
  CHECK(validate(b.g) == GraphError::kUnreachableState);
}

TEST_CASE("a state with no timeout and no conditions is a dead end") {
  Builder b;
  const uint8_t stuck = b.state();
  b.g.entry = stuck;
  CHECK(validate(b.g) == GraphError::kNoTerminal);
}

TEST_CASE("every error has a message") {
  const GraphError all[] = {
      GraphError::kNone,           GraphError::kTooManyStates, GraphError::kTooManyConditions,
      GraphError::kTooManyActions, GraphError::kTooManyDists,  GraphError::kBadEntry,
      GraphError::kBadTarget,      GraphError::kNoTerminal,    GraphError::kUnreachableState};
  for (GraphError e : all) {
    const char* m = graph_error_str(e);
    REQUIRE(m != nullptr);
    CHECK(m[0] != '\0');
  }
}

TEST_CASE("outcome codes are the .tdr wire contract") {
  // These values are in every .tdr the lab has written and every analysis
  // script that reads one. If this test fails, someone renumbered them.
  CHECK(static_cast<int>(Outcome::kUndetermined) == -1);
  CHECK(static_cast<int>(Outcome::kNotStarted) == 0);
  CHECK(static_cast<int>(Outcome::kHit) == 1);
  CHECK(static_cast<int>(Outcome::kWrongResponse) == 2);
  CHECK(static_cast<int>(Outcome::kEarlyHit) == 3);
  CHECK(static_cast<int>(Outcome::kEarlyWrongResponse) == 4);
  CHECK(static_cast<int>(Outcome::kEarly) == 5);
  CHECK(static_cast<int>(Outcome::kLate) == 6);
  CHECK(static_cast<int>(Outcome::kEyeError) == 7);
  CHECK(static_cast<int>(Outcome::kInexpectedStartSignal) == 8);
  CHECK(static_cast<int>(Outcome::kWrongStartSignal) == 9);
  CHECK(static_cast<int>(Outcome::kCancelled) == 10);
}
