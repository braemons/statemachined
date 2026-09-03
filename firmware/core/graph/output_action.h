// SPDX-License-Identifier: GPL-3.0-or-later
// What a state does to the output lines, and what the machine hands back for
// the HAL to apply. The machine never touches a pin: it returns an OutputUpdate
// and the HAL drives it, which is what lets the whole thing run on the host.
#pragma once
#include <cstdint>

#include "config.h"

namespace fsmd {

enum class OutputActionKind : uint8_t { High = 0, Low = 1, Toggle = 2, Pulse = 3 };

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

}  // namespace fsmd
