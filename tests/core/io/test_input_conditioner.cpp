// SPDX-License-Identifier: GPL-3.0-or-later
// Debounce, polarity and enable. All three run before the state machine sees a
// bit, and all three are tested here rather than four times over in four HALs.
#include "doctest.h"
#include "helpers.h"
#include "io/input_conditioner.h"

using namespace statemachined;
using namespace statemachined::test;

namespace {
InputConfig plain(NarrowMilliseconds debounce = 0) {
  InputConfig c;
  for (uint8_t i = 0; i < kMaxLines; ++i) c.debounce_ms[i] = debounce;
  return c;
}
}  // namespace

TEST_CASE("with no debounce the word passes straight through") {
  InputConditioner c;
  c.configure(plain(0));
  CHECK(c.apply(bit(3), 0) == bit(3));
  CHECK(c.apply(0, 100) == 0);
  CHECK(c.settling() == 0);
}

TEST_CASE("inverted lines read as pressed when the pin is low") {
  // Opto-isolators invert. Pushing it into word assembly means no graph, and no
  // predicate, ever has to know.
  InputConfig cfg = plain();
  cfg.invert_mask = bit(2);
  InputConditioner c;
  c.configure(cfg);
  CHECK(c.apply(0, 0) == bit(2));
  CHECK(c.apply(bit(2), 100) == 0);
}

TEST_CASE("a disabled line reads zero whatever its polarity") {
  // Which is why invert runs first and enable second: an inverted line that is
  // not wired to anything would otherwise read as permanently pressed.
  InputConfig cfg = plain();
  cfg.invert_mask = bit(2);
  cfg.enable_mask = ~bit(2);
  InputConditioner c;
  c.configure(cfg);
  CHECK(c.apply(0, 0) == 0);
  CHECK(c.apply(0xFFFFFFFFu, 100) == (0xFFFFFFFFu & ~bit(2)));
}

TEST_CASE("a change is accepted only after it has held for the debounce time") {
  InputConditioner c;
  c.configure(plain(5));  // 5 ms

  CHECK(c.apply(bit(0), 0) == 0);  // seen, not yet believed
  CHECK(c.settling() == bit(0));
  CHECK(c.apply(bit(0), ms(4)) == 0);
  CHECK(c.apply(bit(0), ms(5)) == bit(0));
  CHECK(c.settling() == 0);
}

TEST_CASE("a bounce inside the debounce window abandons the wait") {
  InputConditioner c;
  c.configure(plain(5));

  CHECK(c.apply(bit(0), 0) == 0);
  CHECK(c.apply(0, ms(2)) == 0);  // fell back: not a press
  CHECK(c.settling() == 0);
  CHECK(c.apply(bit(0), ms(3)) == 0);  // the clock restarts here, not at 0
  CHECK(c.apply(bit(0), ms(7)) == 0);  // 4 ms in, so still not believed
  CHECK(c.apply(bit(0), ms(8)) == bit(0));
}

TEST_CASE("debounce applies to the release as well as the press") {
  // A release that is not debounced is a release that chatters, and "none of
  // these lines" is a predicate like any other.
  InputConditioner c;
  c.configure(plain(5));
  REQUIRE(c.apply(bit(1), 0) == 0);
  REQUIRE(c.apply(bit(1), ms(5)) == bit(1));

  CHECK(c.apply(0, ms(6)) == bit(1));
  CHECK(c.apply(0, ms(10)) == bit(1));
  CHECK(c.apply(0, ms(11)) == 0);
}

TEST_CASE("each line debounces on its own clock") {
  InputConfig cfg = plain();
  cfg.debounce_ms[0] = 2;
  cfg.debounce_ms[1] = 20;
  InputConditioner c;
  c.configure(cfg);

  CHECK(c.apply(bit(0) | bit(1), 0) == 0);
  CHECK(c.apply(bit(0) | bit(1), ms(2)) == bit(0));
  CHECK(c.apply(bit(0) | bit(1), ms(19)) == bit(0));
  CHECK(c.apply(bit(0) | bit(1), ms(20)) == (bit(0) | bit(1)));
}

TEST_CASE("prime adopts the pins as they are, with no settling") {
  // Without it, every line that happens to be high at reset looks like it just
  // moved, and a graph waiting on "lever down" would fire on the first scan.
  InputConditioner c;
  c.configure(plain(5));
  c.prime(bit(4) | bit(6));
  CHECK(c.word() == (bit(4) | bit(6)));
  CHECK(c.settling() == 0);
  CHECK(c.apply(bit(4) | bit(6), 0) == (bit(4) | bit(6)));
}

TEST_CASE("debouncing survives the microsecond counter wrapping") {
  // The counter wraps every ~71 minutes and a session is longer than that.
  InputConditioner c;
  c.configure(plain(5));
  const Microseconds near_wrap = 0xFFFFFFFFu - ms(2);
  CHECK(c.apply(bit(0), near_wrap) == 0);
  CHECK(c.apply(bit(0), near_wrap + ms(2)) == 0);  // wrapped past zero
  CHECK(c.apply(bit(0), near_wrap + ms(5)) == bit(0));
}
