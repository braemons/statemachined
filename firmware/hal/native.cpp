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

// No pins, and the labels say so rather than borrowing a board's.
//
// A host build could plausibly answer "D2" here and let the integration tests
// look more like a rig. It would be a lie of exactly the kind the `pins`
// command exists to prevent: nothing here is wired to anything. "sim0" is
// honest, and it still exercises the whole path -- a host that resolves a
// config's `pin = "sim0"` against this device is doing precisely what it does
// against a board, which is asking rather than assuming.
constexpr const char* kSimulatedLineLabels[] = {
    "sim0",  "sim1",  "sim2",  "sim3",  "sim4",  "sim5",  "sim6",  "sim7",
    "sim8",  "sim9",  "sim10", "sim11", "sim12", "sim13", "sim14", "sim15",
    "sim16", "sim17", "sim18", "sim19", "sim20", "sim21", "sim22", "sim23",
    "sim24", "sim25", "sim26", "sim27", "sim28", "sim29", "sim30", "sim31",
};

// One table for both directions: input line 3 and output line 3 are different
// things on a board and neither of them is here, so inventing two sets of names
// would only suggest otherwise.
static_assert(sizeof(kSimulatedLineLabels) / sizeof(kSimulatedLineLabels[0]) >= kMaxLines,
              "every input line this build reports needs a label");
static_assert(sizeof(kSimulatedLineLabels) / sizeof(kSimulatedLineLabels[0]) >= kMaxOutputLines,
              "every output line this build reports needs a label");

const char* const* input_pin_labels() { return kSimulatedLineLabels; }
const char* const* output_pin_labels() { return kSimulatedLineLabels; }

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

size_t link_write_some(const char* src, size_t n) {
  // stdout, so "as much as the link will take right now" is all of it. A pipe
  // whose reader has stopped can still block here; on the host there is no
  // timer whose periods that would cost, and a native runner that hangs on a
  // full pipe is a visible failure rather than a silent one.
  const size_t wrote = std::fwrite(src, 1, n, stdout);
  std::fflush(stdout);
  return wrote;
}

bool link_up() { return true; }

// ------------------------------------------------------ the rig, simulated ---

void set_native_inputs(LineBitmask word) { inputs_ = word; }
LineBitmask native_output_levels() { return levels_; }

}  // namespace hal
}  // namespace statemachined

#endif  // !ARDUINO
