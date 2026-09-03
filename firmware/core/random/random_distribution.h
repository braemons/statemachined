// SPDX-License-Identifier: GPL-3.0-or-later
// Timing distributions: a duration a graph declares is a draw, not a number.
//
// The draw happens on state entry, on the device, from the trial's own stream.
// Integer-only inverse CDF for the truncated exponential -- a float path would
// make the native simulator's numbers merely *close* to the firmware's, which is
// worse than useless for replay.
//
// See dev/PLAN.md, "Randomised timings, drawn on the device".
#pragma once
#include <cstdint>

#include "config.h"
#include "random/rng.h"

namespace fsmd {

enum class RandomDistributionKind : uint8_t {
  Fixed = 0,
  Uniform = 1,
  Exponential = 2,  // truncated: min, max, mean -- flat hazard
  Choice = 3,
};

struct RandomDistribution {
  RandomDistributionKind kind = RandomDistributionKind::Fixed;
  uint8_t n = 0;                       ///< Choice: number of options
  Milliseconds a = 0;                  ///< Fixed: ms. Uniform/Exponential: min_ms
  Milliseconds b = 0;                  ///< Uniform/Exponential: max_ms
  Milliseconds c = 0;                  ///< Exponential: mean_ms
  const Milliseconds* opts = nullptr;  ///< Choice
  const uint16_t* weights = nullptr;   ///< Choice, optional

  /// Draw a duration in milliseconds. Called on state entry.
  Milliseconds draw(Rng& rng) const;
};

}  // namespace fsmd
