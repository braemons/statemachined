// SPDX-License-Identifier: GPL-3.0-or-later
// What is wired to the box, as opposed to what the paradigm does with it.
//
// Invert, enable, debounce and the output safe levels used to live inside
// StateGraph, and re-sending them with every upload made "change the debounce"
// read as "re-upload the graph". They describe the *rig* -- which pin is
// active-low, which lines are connected at all, which level is safe for a valve
// driver -- and a rig is rewired once and then not again for a year, while the
// paradigm above it changes weekly.
//
// The move is also a fail-safe fix, and that is the load-bearing half. main.cpp
// drives every output to its safe level before the first scan, and a rig image
// holds no graph at reset -- so with the safe levels living in a graph there
// was nothing to read, every output went low, and an active-low valve driver
// was opened by every power cycle. See dev/DAEMON.md 3.4.
#pragma once
#include <cstdint>

#include "config.h"

namespace statemachined {

/// Per-line input conditioning, applied when the word is assembled so that every
/// predicate sees clean, polarity-normalised bits and no transition logic has a
/// special case. `invert` is Bpod's logicHigh/logicLow: opto-isolated inputs are
/// routinely active-low.
struct InputConfig {
  LineBitmask invert_mask = 0;
  LineBitmask enable_mask = 0xFFFFFFFF;
  NarrowMilliseconds debounce_ms[kMaxLines] = {0};
};

/// Everything about this board's wiring, which no graph gets a say in.
///
/// The defaults are the compile-time ones: `kCompiledSafeLevels` is what a rig
/// image is built with (`-DSTATEMACHINED_SAFE_LEVELS=0x...`), so a board that
/// has never been told anything still fails safe correctly. A `wiring` command
/// replaces them at runtime; until dev/DAEMON.md's M7 adds data flash, that
/// runtime value does not survive a power cycle and the compiled one is what
/// holds the hole shut.
struct DeviceWiring {
  InputConfig inputs;

  /// Levels outputs are driven to on watchdog timeout, reset, link loss or a
  /// refused graph. Per line, because "off" is not always "low".
  LineBitmask output_safe_levels = kCompiledSafeLevels;
};

}  // namespace statemachined
