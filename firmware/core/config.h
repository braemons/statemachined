// Compile-time capacities. Sized per board: the Uno R4 Minima (32 KB SRAM) is
// the reference target, so these defaults are its numbers. A graph exceeding
// any of them is refused at upload with a message naming what overflowed --
// never at trial 300. See dev/PLAN.md, "Fitting on 32 KB".
#pragma once
#include <cstdint>

#ifndef FSMD_MAX_STATES
#define FSMD_MAX_STATES 32
#endif
#ifndef FSMD_MAX_TRANSITIONS
#define FSMD_MAX_TRANSITIONS 64
#endif
#ifndef FSMD_MAX_ACTIONS
#define FSMD_MAX_ACTIONS 64
#endif
#ifndef FSMD_MAX_DISTRIBUTIONS
#define FSMD_MAX_DISTRIBUTIONS 32
#endif
#ifndef FSMD_MAX_LINES
#define FSMD_MAX_LINES 32  // one uint32_t input word; widening is a type change
#endif
#ifndef FSMD_MAX_OUTPUT_LINES
#define FSMD_MAX_OUTPUT_LINES 32  // one LineBitmask of outputs; a line at or
#endif                            // past this cannot be represented at all
#ifndef FSMD_MAX_PATH
#define FSMD_MAX_PATH 64  // ring buffer: a graph may loop, and a long trial
#endif                    // must degrade to a truncated path, never a corrupt one

namespace fsmd {
constexpr uint8_t kMaxStates = FSMD_MAX_STATES;
constexpr uint8_t kMaxTransitions = FSMD_MAX_TRANSITIONS;
constexpr uint8_t kMaxActions = FSMD_MAX_ACTIONS;
constexpr uint8_t kMaxDistributions = FSMD_MAX_DISTRIBUTIONS;
constexpr uint8_t kMaxLines = FSMD_MAX_LINES;

/// Output lines are a separate index space from input lines and need their own
/// bound: an action naming a line at or past this cannot be represented in a
/// LineBitmask at all, and shifting by it is undefined behaviour.
constexpr uint8_t kMaxOutputLines = FSMD_MAX_OUTPUT_LINES;
constexpr uint8_t kMaxPath = FSMD_MAX_PATH;
constexpr uint8_t kNoState = 0xFF;

/// A set of I/O lines, one bit per line. Named because a bare uint32_t here is
/// indistinguishable from a duration or a count, and the masks, the input word,
/// the output updates and the safe levels are all this and nothing else.
using LineBitmask = uint32_t;

/// Index into StateGraph::distributions. Timeouts and holds are drawn from the
/// shared pool rather than stored inline, so a graph can reuse one distribution
/// everywhere it means the same thing.
using DistributionIndex = uint8_t;
constexpr DistributionIndex kNoDistribution = 0xFF;

/// "no transition fired" in a StateVisit, for an exit that was not a guard.
constexpr uint8_t kNoTransition = 0xFF;

/// Time. The two units are never interchangeable: the device clock ticks in
/// microseconds and everything a graph declares is in milliseconds, so a bare
/// integer at a call boundary is a bug waiting to happen.
using Microseconds = uint32_t;  ///< device clock; wraps every ~71 minutes
using Milliseconds = int32_t;   ///< signed: a negative value means "none set"

/// Milliseconds narrowed where the graph stores one per line or per action and
/// the width is worth the SRAM on a 32 KB board.
using NarrowMilliseconds = uint16_t;
}  // namespace fsmd
