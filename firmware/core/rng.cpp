#include "rng.h"

namespace fsmd {
namespace {

constexpr uint32_t rotl(uint32_t x, int k) { return (x << k) | (x >> (32 - k)); }

/// SplitMix64 -- used only to expand a seed into xoshiro's state, which is what
/// the reference implementation recommends. A xoshiro state of all zeros is a
/// fixed point, so the expansion also guarantees we never start there.
uint64_t splitmix64(uint64_t& x) {
  uint64_t z = (x += 0x9E3779B97F4A7C15ull);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
  return z ^ (z >> 31);
}

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

void Rng::reseed(uint64_t seed) {
  uint64_t x = seed ? seed : 0x853C49E6748FEA9Bull;
  uint64_t a = splitmix64(x);
  uint64_t b = splitmix64(x);
  s_[0] = static_cast<uint32_t>(a);
  s_[1] = static_cast<uint32_t>(a >> 32);
  s_[2] = static_cast<uint32_t>(b);
  s_[3] = static_cast<uint32_t>(b >> 32);
}

uint64_t Rng::mix(uint64_t session_seed, uint32_t trial_id) {
  uint64_t x = session_seed ^ (static_cast<uint64_t>(trial_id) * 0x9E3779B97F4A7C15ull);
  return splitmix64(x);
}

uint32_t Rng::next_u32() {
  const uint32_t result = rotl(s_[1] * 5u, 7) * 9u;
  const uint32_t t = s_[1] << 9;
  s_[2] ^= s_[0];
  s_[3] ^= s_[1];
  s_[1] ^= s_[2];
  s_[0] ^= s_[3];
  s_[2] ^= t;
  s_[3] = rotl(s_[3], 11);
  return result;
}

uint32_t Rng::below(uint32_t bound) {
  if (bound == 0) return 0;
  // Lemire: multiply into the high half and reject only the short tail, so the
  // common path costs one multiply and no division. Unbiased, unlike `% n`.
  uint64_t m = static_cast<uint64_t>(next_u32()) * bound;
  uint32_t low = static_cast<uint32_t>(m);
  if (low < bound) {
    const uint32_t threshold = (0u - bound) % bound;
    while (low < threshold) {
      m = static_cast<uint64_t>(next_u32()) * bound;
      low = static_cast<uint32_t>(m);
    }
  }
  return static_cast<uint32_t>(m >> 32);
}

int32_t Rng::between(int32_t lo, int32_t hi) {
  if (hi <= lo) return lo;
  const uint32_t span = static_cast<uint32_t>(hi - lo) + 1u;
  return lo + static_cast<int32_t>(below(span));
}

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
