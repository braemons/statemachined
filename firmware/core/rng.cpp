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

}  // namespace fsmd
