// SPDX-License-Identifier: GPL-3.0-or-later
// Compile-time capacities. Sized per board: the Uno R4 Minima (32 KB SRAM) is
// the reference target, so these defaults are its numbers. A graph exceeding
// any of them is refused at upload with a message naming what overflowed --
// never at trial 300. See dev/PLAN.md, "Fitting on 32 KB".
#pragma once
#include <cstdint>

// How many graphs one set may hold. Sixty bytes of table on the reference
// board, so this is not where a set's cost is -- the pools below are. A session
// declares its graphs up front and switches between them by index; see
// dev/DAEMON.md 3.2.
#ifndef STATEMACHINED_MAX_GRAPHS
#define STATEMACHINED_MAX_GRAPHS 20
#endif
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
// The levels a rig's outputs are safe at, as a bitmask, compiled in. Zero --
// every line low -- is right for a bench and wrong for any rig with an
// active-low driver on it, which is why `make firmware-rig` is where a real one
// belongs: -DSTATEMACHINED_SAFE_LEVELS=0x...
//
// It is the *default*, not the truth: a `wiring` command replaces it, and
// dev/DAEMON.md M7 will persist that to data flash. Until then this is the only
// thing that makes the fail_safe() before the first scan correct on a board
// nobody has greeted yet, and it must stay so -- a mitigation that depends on
// the daemon being up is not one.
#ifndef STATEMACHINED_SAFE_LEVELS
#define STATEMACHINED_SAFE_LEVELS 0
#endif
// Demo mode: the graph a board runs before anybody greets it, so that a bench
// board with a switch and a few LEDs does something you can watch. On by
// default; `make firmware-rig` builds it out and gets its ~4.6 KB back. It is
// here rather than in main.cpp because it is a capacity decision -- it costs a
// second StateGraph and a second TrialRunner -- and the path length below now
// depends on it.
#ifndef STATEMACHINED_DEMO
#define STATEMACHINED_DEMO 1
#endif

// The visit ring: a graph may loop, and a long trial must degrade to a
// truncated path, never a corrupt one.
//
// It was briefly 64 on the bench image, because demo mode carries a second
// TrialRunner and 2 x 4080 B would not link. The graph set paid that back: a
// set is single-buffered, so the staged copy a single graph needed is gone, and
// both images fit 255 again. Both boards therefore report the same capacities,
// which is the state worth being in.
#ifndef STATEMACHINED_MAX_PATH
#define STATEMACHINED_MAX_PATH 255
#endif

namespace statemachined {
constexpr uint8_t kMaxGraphs = STATEMACHINED_MAX_GRAPHS;
constexpr uint8_t kMaxStates = STATEMACHINED_MAX_STATES;
constexpr uint8_t kMaxTransitions = STATEMACHINED_MAX_TRANSITIONS;
constexpr uint8_t kMaxOutputActions = STATEMACHINED_MAX_OUTPUT_ACTIONS;
constexpr uint8_t kMaxDistributions = STATEMACHINED_MAX_DISTRIBUTIONS;
constexpr uint8_t kMaxLines = STATEMACHINED_MAX_LINES;

/// Output lines are a separate index space from input lines and need their own
/// bound: an action naming a line at or past this cannot be represented in a
/// LineBitmask at all, and shifting by it is undefined behaviour.
constexpr uint8_t kMaxOutputLines = STATEMACHINED_MAX_OUTPUT_LINES;
/// 255 is not a compromise between two costs; it is where the type stops the
/// count. `path_len` is a uint8_t, so 255 is the largest value record_visit's
/// comparison still terminates on and 256 is the value that breaks it silently.
/// Going past it means widening path_len, result_begin's path_len and the
/// `from` index of every result_path chunk -- a protocol change for headroom
/// nobody has asked for. At 16 B a StateVisit that is 4080 B, about 3 KB more
/// than the 64 it used to be, which is the price of a trial whose path is
/// complete rather than truncated. See dev/DAEMON.md 3.6.
constexpr uint8_t kMaxPath = STATEMACHINED_MAX_PATH;
static_assert(kMaxPath >= 1 && STATEMACHINED_MAX_PATH <= 255,
              "path_len is a uint8_t; 256 breaks record_visit's comparison silently");

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

/// The output safe levels this binary was built with. See
/// STATEMACHINED_SAFE_LEVELS above: it is what a board fails safe to before
/// anything has told it anything.
constexpr LineBitmask kCompiledSafeLevels = STATEMACHINED_SAFE_LEVELS;

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
