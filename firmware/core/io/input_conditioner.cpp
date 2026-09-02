#include "io/input_conditioner.h"

namespace fsmd {
namespace {
/// Wraparound-safe elapsed time; the microsecond counter wraps every ~71
/// minutes and a lever press must not care.
inline uint32_t since(uint32_t from, uint32_t now) { return now - from; }

inline LineBitmask condition(const InputConfig& cfg, LineBitmask raw) {
  // Invert first, then mask: a disabled line reads 0 whatever its polarity,
  // which is what "disabled" has to mean for an active-low input that is not
  // wired to anything.
  return (raw ^ cfg.invert_mask) & cfg.enable_mask;
}
}  // namespace

void InputConditioner::prime(LineBitmask raw) {
  stable_ = condition(cfg_, raw);
  pending_ = 0;
}

LineBitmask InputConditioner::apply(LineBitmask raw, Microseconds now_us) {
  const LineBitmask norm = condition(cfg_, raw);
  const LineBitmask differing = norm ^ stable_;

  // A line that agrees again abandons its wait. That is the whole of debounce:
  // a bounce is a disagreement that did not last.
  pending_ &= differing;

  // Lines that have only just started to disagree: start their clocks.
  const LineBitmask fresh = differing & ~pending_;
  for (LineBitmask rest = fresh; rest != 0;) {
    const uint8_t line = static_cast<uint8_t>(__builtin_ctz(rest));
    rest &= ~(1u << line);
    since_us_[line] = now_us;
  }
  pending_ |= fresh;

  for (LineBitmask rest = pending_; rest != 0;) {
    const uint8_t line = static_cast<uint8_t>(__builtin_ctz(rest));
    const LineBitmask bit = 1u << line;
    rest &= ~bit;
    if (since(since_us_[line], now_us) >=
        static_cast<uint32_t>(cfg_.debounce_ms[line]) * 1000u) {
      stable_ ^= bit;  // held long enough: accept the new level
      pending_ &= ~bit;
    }
  }

  return stable_;
}

}  // namespace fsmd
