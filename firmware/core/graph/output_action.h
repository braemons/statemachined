// SPDX-License-Identifier: GPL-3.0-or-later
// What a state does to the output lines, and what the machine hands back for
// the HAL to apply. The machine never touches a pin: it returns an OutputUpdate
// and the HAL drives it, which is what lets the whole thing run on the host.
#pragma once
#include <cstdint>

#include "config.h"

namespace statemachined {

/// TimerStart and TimerCancel are Bpod's idea, and dev/PLAN.md endorses it: its
/// output matrix carries global-timer triggers as pseudo-outputs so that there
/// is one action vocabulary rather than two. They cost nothing here -- a timer
/// index fits in the `output_line` field an action already has.
///
/// A timer can also be started by a line going high (graph/global_timer.h's
/// trigger masks, which is VStim's way in). Both exist because they answer
/// different questions: "this state starts the foreperiod timer" is an action,
/// and "the foreperiod timer starts when the house light does" is a predicate,
/// and writing the second as the first would mean burning a state on it.
enum class OutputActionKind : uint8_t {
  High = 0,
  Low = 1,
  Toggle = 2,
  Pulse = 3,
  TimerStart = 4,   ///< `output_line` is a timer index, not a line
  TimerCancel = 5,  ///< likewise
};

/// True for the kinds whose `output_line` is a timer index rather than a line.
/// Every bound check on an action has to ask, because the two index spaces have
/// different sizes and a timer index validated against kMaxOutputLines would
/// let a set name a timer that does not exist.
constexpr bool addresses_a_timer(OutputActionKind k) {
  return k == OutputActionKind::TimerStart || k == OutputActionKind::TimerCancel;
}

struct OutputAction {
  LineIndex output_line = 0;
  OutputActionKind kind = OutputActionKind::High;
  NarrowMilliseconds pulse_ms = 0;  ///< Pulse only
};

/// The lines to move this scan. Both masks, rather than one level word, because
/// a scan that touches nothing must be distinguishable from one that drives
/// everything low.
struct OutputUpdate {
  LineBitmask set_high = 0;  ///< lines to drive high this scan
  LineBitmask set_low = 0;   ///< lines to drive low this scan
};

}  // namespace statemachined
