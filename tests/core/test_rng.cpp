// The PRNG carries the reproducibility claim, so these tests are stricter than
// they look: a change that makes any of them fail changes recorded sessions.
#include <map>
#include <vector>

#include "doctest.h"
#include "rng.h"

using namespace fsmd;

TEST_CASE("the same seed gives the same sequence") {
  Rng a(12345), b(12345);
  for (int i = 0; i < 1000; ++i) REQUIRE(a.next_u32() == b.next_u32());
}

TEST_CASE("different seeds diverge immediately") {
  Rng a(1), b(2);
  CHECK(a.next_u32() != b.next_u32());
}

TEST_CASE("a zero seed is not a fixed point") {
  // xoshiro's all-zero state produces only zeros. The SplitMix expansion in
  // reseed() is what prevents it; this test is the reason that code exists.
  Rng z(0);
  bool any_nonzero = false;
  for (int i = 0; i < 16; ++i) any_nonzero |= (z.next_u32() != 0);
  CHECK(any_nonzero);
}

TEST_CASE("golden vector: the sequence is frozen") {
  // If this fails, every previously recorded session replays differently.
  // Changing it is a wire-contract change, not a refactor.
  //
  // This test must also pass ON EVERY BOARD, not only on the host -- that is
  // the whole cross-board reproducibility claim, and it is why the PRNG is
  // ours rather than the core's random().
  Rng r(0xDEADBEEF);
  const uint32_t got[4] = {r.next_u32(), r.next_u32(), r.next_u32(), r.next_u32()};
  const uint32_t expected[4] = {2848183187u, 2643428161u, 2173202904u, 2114983966u};
  for (int i = 0; i < 4; ++i) CHECK(got[i] == expected[i]);
}

TEST_CASE("mix() derives a per-trial stream from the session seed") {
  // Derived rather than free-running: replaying trial 412 alone must draw
  // trial 412's numbers, and a link reset must desynchronise nothing.
  const uint64_t session = 0x5EED;
  CHECK(Rng::mix(session, 412) == Rng::mix(session, 412));
  CHECK(Rng::mix(session, 412) != Rng::mix(session, 413));
  CHECK(Rng::mix(session, 412) != Rng::mix(session + 1, 412));

  Rng a(Rng::mix(session, 412));
  Rng b(Rng::mix(session, 412));
  for (int i = 0; i < 64; ++i) REQUIRE(a.next_u32() == b.next_u32());
}

TEST_CASE("below() respects its bound and is unbiased") {
  Rng r(7);
  CHECK(r.below(0) == 0);
  CHECK(r.below(1) == 0);

  // The bias `% n` would introduce is largest when the bound is just over a
  // power of two, so test there. triald's CLAUDE.md calls out VStim's
  // `rand() % n` for exactly this.
  constexpr uint32_t kBound = 3;
  constexpr int kDraws = 300000;
  int counts[kBound] = {0};
  for (int i = 0; i < kDraws; ++i) {
    const uint32_t v = r.below(kBound);
    REQUIRE(v < kBound);
    counts[v]++;
  }
  const double expect = static_cast<double>(kDraws) / kBound;
  for (uint32_t i = 0; i < kBound; ++i) {
    const double dev = (counts[i] - expect) / expect;
    CHECK(dev < 0.02);
    CHECK(dev > -0.02);
  }
}

TEST_CASE("between() is inclusive at both ends") {
  Rng r(99);
  CHECK(r.between(5, 5) == 5);
  CHECK(r.between(9, 3) == 9);  // degenerate range returns lo

  bool saw_lo = false, saw_hi = false;
  for (int i = 0; i < 10000; ++i) {
    const int32_t v = r.between(10, 12);
    REQUIRE(v >= 10);
    REQUIRE(v <= 12);
    saw_lo |= (v == 10);
    saw_hi |= (v == 12);
  }
  CHECK(saw_lo);
  CHECK(saw_hi);
}

TEST_CASE("fixed distribution returns its value") {
  Rng r(1);
  Dist d;
  d.kind = DistKind::kFixed;
  d.a = 250;
  for (int i = 0; i < 32; ++i) CHECK(d.draw(r) == 250);
}

TEST_CASE("uniform distribution stays in range and covers it") {
  Rng r(2);
  Dist d;
  d.kind = DistKind::kUniform;
  d.a = 500;
  d.b = 3000;
  int64_t sum = 0;
  constexpr int kN = 20000;
  for (int i = 0; i < kN; ++i) {
    const int32_t v = d.draw(r);
    REQUIRE(v >= 500);
    REQUIRE(v <= 3000);
    sum += v;
  }
  const double mean = static_cast<double>(sum) / kN;
  CHECK(mean > 1700.0);
  CHECK(mean < 1800.0);  // true mean 1750
}

TEST_CASE("truncated exponential is bounded, integer-only, and flat-hazard") {
  Rng r(3);
  Dist d;
  d.kind = DistKind::kExponential;
  d.a = 500;
  d.b = 3000;
  d.c = 1200;

  constexpr int kN = 50000;
  std::vector<int32_t> v;
  v.reserve(kN);
  for (int i = 0; i < kN; ++i) {
    const int32_t x = d.draw(r);
    REQUIRE(x >= 500);
    REQUIRE(x <= 3000);
    v.push_back(x);
  }

  // Flat hazard is the property that matters behaviourally: given that the
  // foreperiod has not yet elapsed, the chance it elapses in the next slice
  // should not grow with elapsed time. Compare the hazard early and late.
  auto hazard = [&](int32_t lo, int32_t hi) {
    int at_risk = 0, events = 0;
    for (int32_t x : v) {
      if (x >= lo) {
        at_risk++;
        if (x < hi) events++;
      }
    }
    return at_risk ? static_cast<double>(events) / at_risk : 0.0;
  };
  const double early = hazard(600, 900);
  const double late = hazard(1800, 2100);
  CHECK(late > early * 0.6);
  CHECK(late < early * 1.6);
}

TEST_CASE("exponential degenerates safely") {
  Rng r(4);
  Dist d;
  d.kind = DistKind::kExponential;
  d.a = 100;
  d.b = 100;
  d.c = 50;
  CHECK(d.draw(r) == 100);

  d.b = 500;
  d.c = 0;  // no mean: fall back to uniform rather than divide by zero
  const int32_t x = d.draw(r);
  CHECK(x >= 100);
  CHECK(x <= 500);
}

TEST_CASE("choice distribution draws only from its options") {
  Rng r(5);
  const int32_t opts[3] = {100, 200, 400};
  Dist d;
  d.kind = DistKind::kChoice;
  d.n = 3;
  d.opts = opts;

  std::map<int32_t, int> seen;
  for (int i = 0; i < 6000; ++i) seen[d.draw(r)]++;
  CHECK(seen.size() == 3);
  for (auto& [value, count] : seen) {
    CHECK((value == 100 || value == 200 || value == 400));
    CHECK(count > 1500);
  }
}

TEST_CASE("choice distribution honours weights") {
  Rng r(6);
  const int32_t opts[2] = {10, 20};
  const uint16_t weights[2] = {9, 1};
  Dist d;
  d.kind = DistKind::kChoice;
  d.n = 2;
  d.opts = opts;
  d.weights = weights;

  int tens = 0;
  constexpr int kN = 20000;
  for (int i = 0; i < kN; ++i)
    if (d.draw(r) == 10) tens++;
  const double frac = static_cast<double>(tens) / kN;
  CHECK(frac > 0.88);
  CHECK(frac < 0.92);
}

TEST_CASE("a draw is reproducible from the seed alone") {
  // The whole reproducibility claim, in one test: same session seed and same
  // trial id must give the same realised durations.
  const int32_t opts[3] = {1, 2, 3};
  auto run = [&] {
    Rng r(Rng::mix(0xABCDEF, 412));
    Dist u{DistKind::kUniform, 0, 100, 900, 0, nullptr, nullptr};
    Dist e{DistKind::kExponential, 0, 200, 2000, 700, nullptr, nullptr};
    Dist c{DistKind::kChoice, 3, 0, 0, 0, opts, nullptr};
    std::vector<int32_t> out;
    for (int i = 0; i < 50; ++i) {
      out.push_back(u.draw(r));
      out.push_back(e.draw(r));
      out.push_back(c.draw(r));
    }
    return out;
  };
  CHECK(run() == run());
}
