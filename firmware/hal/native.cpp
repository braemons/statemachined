// The host. Not a board, and not a toy either: it is what lets a whole session
// be played out at host speed with no hardware attached, and it is why hal.h is
// compiled by CI rather than only by a board build.
//
// Input lines are driven by set_native_inputs() rather than read from anywhere,
// so a test or a simulator drives the rig. Output lines are recorded. The clock
// is the monotonic one, truncated to 32 bits exactly as a board's is -- if
// something breaks on the wrap every ~71 minutes, it should break here too.
#if !defined(ARDUINO)

#include <cstdio>
#include <ctime>

#include "hal.h"

namespace fsmd {
namespace hal {
namespace {
LineBitmask inputs_ = 0;
LineBitmask levels_ = 0;
}  // namespace

void init() {
  levels_ = 0;
  inputs_ = 0;
}

LineBitmask read_inputs() { return inputs_; }

void write_outputs(LineBitmask set_high, LineBitmask set_low) {
  // Same rule as the board: a line in both is a bug upstream, and it goes high.
  levels_ = (levels_ | set_high) & ~(set_low & ~set_high);
}

Microseconds micros_now() {
  timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  const unsigned long long us =
      static_cast<unsigned long long>(ts.tv_sec) * 1000000ull + ts.tv_nsec / 1000;
  return static_cast<Microseconds>(us);  // truncated, so the wrap is real here too
}

size_t link_read(char* dst, size_t max) {
  if (max == 0) return 0;
  const int c = std::getchar();
  if (c == EOF) return 0;
  dst[0] = static_cast<char>(c);
  return 1;
}

void link_write(const char* src, size_t n) {
  std::fwrite(src, 1, n, stdout);
  std::fflush(stdout);
}

bool link_up() { return true; }

// ------------------------------------------------------ the rig, simulated ---

void set_native_inputs(LineBitmask word) { inputs_ = word; }
LineBitmask native_output_levels() { return levels_; }

}  // namespace hal
}  // namespace fsmd

#endif  // !ARDUINO
