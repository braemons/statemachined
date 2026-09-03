// SPDX-License-Identifier: GPL-3.0-or-later
// Compile-time capacities. Sized per board: the Uno R4 Minima (32 KB SRAM) is
// the reference target, so these defaults are its numbers. A graph exceeding
// any of them is refused at upload with a message naming what overflowed --
// never at trial 300. See dev/PLAN.md, "Fitting on 32 KB".
#pragma once
#include <cstdint>

#ifndef STATEMACHINED_MAX_STATES
#define STATEMACHINED_MAX_STATES 32
#endif
#ifndef STATEMACHINED_MAX_TRANSITIONS
#define STATEMACHINED_MAX_TRANSITIONS 64
#endif
#ifndef STATEMACHINED_MAX_OUTPUT_ACTIONS
#define STATEMACHINED_MAX_OUTPUT_ACTIONS 64
#endif
#ifndef STATEMACHINED_MAX_DISTRIBUTIONS
#define STATEMACHINED_MAX_DISTRIBUTIONS 32
#endif
#ifndef STATEMACHINED_MAX_CHOICE_OPTIONS
#define STATEMACHINED_MAX_CHOICE_OPTIONS 32  // pooled across every Choice distribution
#endif
#ifndef STATEMACHINED_MAX_LINES
#define STATEMACHINED_MAX_LINES 32  // one uint32_t input word; widening is a type change
#endif
#ifndef STATEMACHINED_MAX_OUTPUT_LINES
#define STATEMACHINED_MAX_OUTPUT_LINES 32  // one LineBitmask of outputs; a line at or
#endif                                     // past this cannot be represented at all
#ifndef STATEMACHINED_MAX_LINE
#define STATEMACHINED_MAX_LINE 512  // one protocol line, newline included. The largest
#endif                              // single message is one state's worth of graph
#ifndef STATEMACHINED_MAX_PATH
#define STATEMACHINED_MAX_PATH 64  // ring buffer: a graph may loop, and a long trial
#endif                             // must degrade to a truncated path, never a corrupt one

namespace statemachined {
constexpr uint8_t kMaxStates = STATEMACHINED_MAX_STATES;
constexpr uint8_t kMaxTransitions = STATEMACHINED_MAX_TRANSITIONS;
constexpr uint8_t kMaxOutputActions = STATEMACHINED_MAX_OUTPUT_ACTIONS;
constexpr uint8_t kMaxDistributions = STATEMACHINED_MAX_DISTRIBUTIONS;
constexpr uint8_t kMaxLines = STATEMACHINED_MAX_LINES;

/// Output lines are a separate index space from input lines and need their own
/// bound: an action naming a line at or past this cannot be represented in a
/// LineBitmask at all, and shifting by it is undefined behaviour.
constexpr uint8_t kMaxOutputLines = STATEMACHINED_MAX_OUTPUT_LINES;
constexpr uint8_t kMaxPath = STATEMACHINED_MAX_PATH;

/// The protocol's line budget, reported to the host in hello_ack. Every message
/// is sized to fit inside it -- which is why the graph upload and the trial
/// result are both chunked. See dev/PROTOCOL.md.
constexpr uint16_t kMaxLine = STATEMACHINED_MAX_LINE;

/// Choice options and their weights live in one shared pool, like everything
/// else a graph refers to by index, so a distribution can be uploaded without
/// the device having to find somewhere to put its array.
constexpr uint8_t kMaxChoiceOptions = STATEMACHINED_MAX_CHOICE_OPTIONS;
/// Indices into the StateGraph's shared pools. Everything in a graph is stored
/// in one flat array per kind and referred to by position -- that is what keeps
/// a graph inside 32 KB -- so a bare uint8_t crossing a call boundary could be
/// any of four different things. These say which.
using StateIndex = uint8_t;
using LineIndex = uint8_t;  ///< bit position in a LineBitmask, < kMaxOutputLines
using TransitionIndex = uint8_t;
using OutputActionIndex = uint8_t;

constexpr StateIndex kNoState = 0xFF;

/// A set of I/O lines, one bit per line. Named because a bare uint32_t here is
/// indistinguishable from a duration or a count, and the masks, the input word,
/// the output updates and the safe levels are all this and nothing else.
using LineBitmask = uint32_t;

/// Index into StateGraph::distributions. Timeouts and holds are drawn from the
/// shared pool rather than stored inline, so a graph can reuse one distribution
/// everywhere it means the same thing.
using RandomDistributionIndex = uint8_t;
constexpr RandomDistributionIndex kNoRandomDistribution = 0xFF;

/// "no transition fired" in a StateVisit, for an exit that was not a guard.
constexpr TransitionIndex kNoTransition = 0xFF;

/// Time. The two units are never interchangeable: the device clock ticks in
/// microseconds and everything a graph declares is in milliseconds, so a bare
/// integer at a call boundary is a bug waiting to happen.
using Microseconds = uint32_t;  ///< device clock; wraps every ~71 minutes
using Milliseconds = int32_t;   ///< signed: a negative value means "none set"

/// Milliseconds narrowed where the graph stores one per line or per action and
/// the width is worth the SRAM on a 32 KB board.
using NarrowMilliseconds = uint16_t;
}  // namespace statemachined
