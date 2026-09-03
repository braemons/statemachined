// SPDX-License-Identifier: GPL-3.0-or-later
// The host. Not a board, and not a toy either: it is what lets a whole session
// be played out at host speed with no hardware attached, and it is why hal.h is
// compiled by CI rather than only by a board build.
//
// Input lines are driven by set_native_inputs() rather than read from anywhere,
// so a test or a simulator drives the rig. Output lines are recorded. The clock
// is the monotonic one, truncated to 32 bits exactly as a board's is -- if
// something breaks on the wrap every ~71 minutes, it should break here too.
#if !defined(ARDUINO)

#include <fcntl.h>
#include <unistd.h>

#include <cstdio>
#include <ctime>

#include "hal.h"

namespace statemachined {
namespace hal {
namespace {
LineBitmask inputs_ = 0;
LineBitmask levels_ = 0;
}  // namespace

void init() {
  levels_ = 0;
  inputs_ = 0;

  // hal.h promises link_read() never blocks, and on a board that is free --
  // asking a UART peripheral whether a byte is waiting cannot block. Here it
  // has to be arranged, because stdin is a tty or a pipe and a plain read()
  // will happily wait forever for a byte that is not coming.
  //
  // A blocking read here would stall the scan loop of any host-side runner or
  // simulator: it would sit inside service_link() while the trial it is meant
  // to be advancing stood still, which is precisely the failure the two halves
  // of loop() are separated to avoid.
  const int flags = fcntl(STDIN_FILENO, F_GETFL, 0);
  if (flags >= 0) fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK);
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
  // read() rather than getchar(): stdio buffering would reintroduce the block
  // that init() just took the trouble to remove. A return of -1 with EAGAIN is
  // the ordinary "nothing waiting" case and is not distinguished from any other
  // failure, because there is nothing useful this layer could do differently.
  const ssize_t n = ::read(STDIN_FILENO, dst, max);
  return n > 0 ? static_cast<size_t>(n) : 0;
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
}  // namespace statemachined

#endif  // !ARDUINO
