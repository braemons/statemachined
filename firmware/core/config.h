// Compile-time capacities. Sized per board: the Uno R4 Minima (32 KB SRAM) is
// the reference target, so these defaults are its numbers. A graph exceeding
// any of them is refused at upload with a message naming what overflowed --
// never at trial 300. See dev/PLAN.md, "Fitting on 32 KB".
#pragma once
#include <cstdint>

#ifndef FSMD_MAX_STATES
#define FSMD_MAX_STATES 32
#endif
#ifndef FSMD_MAX_CONDITIONS
#define FSMD_MAX_CONDITIONS 64
#endif
#ifndef FSMD_MAX_ACTIONS
#define FSMD_MAX_ACTIONS 64
#endif
#ifndef FSMD_MAX_DISTS
#define FSMD_MAX_DISTS 32
#endif
#ifndef FSMD_MAX_LINES
#define FSMD_MAX_LINES 32  // one uint32_t input word; widening is a type change
#endif
#ifndef FSMD_MAX_PATH
#define FSMD_MAX_PATH 64  // ring buffer: a graph may loop, and a long trial
#endif                    // must degrade to a truncated path, never a corrupt one

namespace fsmd {
constexpr uint8_t kMaxStates = FSMD_MAX_STATES;
constexpr uint8_t kMaxConditions = FSMD_MAX_CONDITIONS;
constexpr uint8_t kMaxActions = FSMD_MAX_ACTIONS;
constexpr uint8_t kMaxDists = FSMD_MAX_DISTS;
constexpr uint8_t kMaxLines = FSMD_MAX_LINES;
constexpr uint8_t kMaxPath = FSMD_MAX_PATH;
constexpr uint8_t kNoState = 0xFF;
}  // namespace fsmd
