#include "random/random_distribution.h"

namespace fsmd {

namespace {

/// Inverse CDF of the unit exponential, as a fixed-point table rather than
/// log(). 33 entries over u in [0, 1], values are -ln(1-u) in Q16.16, clamped
/// at the top where the true function diverges.
///
/// Integer-only so a drawn duration is bit-identical on every board and on the
/// host. See rng.h.
constexpr int32_t kInvExpQ16[33] = {
    0,     2064,  4184,  6364,  8608,  10921,  13307,  15772,  18322,  20964,  23706,
    26556, 29525, 32624, 35867, 39270, 42851,  46632,  50639,  54903,  59461,  64358,
    69649, 75403, 81706, 88670, 96444, 105226, 115295, 127085, 141335, 159487, 184320,
};

}  // namespace

Milliseconds RandomDistribution::draw(Rng& rng) const {
  switch (kind) {
    case RandomDistributionKind::Fixed:
      return a;

    case RandomDistributionKind::Uniform:
      return rng.between(a, b);

    case RandomDistributionKind::Exponential: {
      // Truncated exponential over [a, b] with mean parameter c: a flat hazard
      // rate, so the animal cannot time the go cue from elapsed time alone.
      if (b <= a) return a;
      if (c <= 0) return rng.between(a, b);
      // Interpolate the Q16.16 inverse CDF at u = next_u32() / 2^32.
      const uint32_t u = rng.next_u32();
      const uint32_t idx = u >> 27;              // 0..31
      const uint32_t frac = (u >> 11) & 0xFFFF;  // Q0.16 within the cell
      const int64_t lo = kInvExpQ16[idx];
      const int64_t hi = kInvExpQ16[idx + 1];
      const int64_t q = lo + ((hi - lo) * frac >> 16);  // Q16.16 exponential deviate
      int64_t ms = a + ((q * c) >> 16);
      if (ms > b) ms = b;
      if (ms < a) ms = a;
      return static_cast<int32_t>(ms);
    }

    case RandomDistributionKind::Choice: {
      if (n == 0 || opts == nullptr) return 0;
      if (weights == nullptr) return opts[rng.below(n)];
      uint32_t total = 0;
      for (uint8_t i = 0; i < n; ++i) total += weights[i];
      if (total == 0) return opts[rng.below(n)];
      uint32_t r = rng.below(total);
      for (uint8_t i = 0; i < n; ++i) {
        if (r < weights[i]) return opts[i];
        r -= weights[i];
      }
      return opts[n - 1];
    }
  }
  return a;
}

}  // namespace fsmd
