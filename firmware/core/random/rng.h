// SPDX-License-Identifier: GPL-3.0-or-later
// The deterministic PRNG. The distributions that draw from it are in
// random_distribution.h.
//
// Reproducibility is the whole point, so every choice here is made for it:
//
//   - xoshiro128** rather than Arduino's random(), which is the underlying libc
//     random() and differs between the AVR, Renesas and ESP-IDF cores. The same
//     seed must give the same sequence on every board and on the host.
//   - No hardware TRNG, though the i.MX RT1062 and ESP32 both have one.
//   - Lemire's multiply-shift for bounded draws, not %. triald's CLAUDE.md
//     calls out VStim's rand() % n as biased; repeating that here would be poor.
//   - Integer-only inverse CDF for the truncated exponential. A float path would
//     make the native simulator's numbers merely *close* to the firmware's,
//     which is worse than useless for replay.
//
// See dev/PLAN.md, "Randomised timings, drawn on the device".
#pragma once
#include <cstdint>

#include "config.h"

namespace fsmd {

class Rng {
 public:
  explicit Rng(uint64_t seed = 0) { reseed(seed); }

  void reseed(uint64_t seed);

  /// Per-trial stream, derived rather than free-running: replaying trial 412
  /// alone must draw trial 412's numbers, and a link reset must desynchronise
  /// nothing.
  static uint64_t mix(uint64_t session_seed, uint32_t trial_id);

  uint32_t next_u32();

  /// Uniform in [0, bound). Lemire's method; unbiased, no modulo, no division
  /// in the common path.
  uint32_t below(uint32_t bound);

  /// Uniform in [lo, hi] inclusive.
  int32_t between(int32_t lo, int32_t hi);

 private:
  uint32_t s_[4] = {0, 0, 0, 0};
};

}  // namespace fsmd
