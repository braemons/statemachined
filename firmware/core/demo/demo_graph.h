// SPDX-License-Identifier: GPL-3.0-or-later
// A graph the device already has, so a board with nothing attached but a switch
// and a few LEDs does something you can watch.
//
// Everything else in this firmware waits for a host: the device holds no graph
// until one is uploaded, and nothing arms a trial until a `start` arrives. That
// is right for a rig and unhelpful for a bench, where the first question is
// "does this board work at all, and are my LEDs on the pins I think they are?"
//
// So this is a paradigm-shaped graph that exists only to be visible. It is
// still a *real* graph -- built with the same structs an upload fills in,
// validated by the same validate(), and run by the same TrialRunner -- which is
// what makes watching it evidence about the firmware rather than a light show
// on a parallel code path. It is in core/ rather than in main.cpp precisely so
// the host tests can run it.
//
// It is NOT a fallback paradigm. The instant a host says `hello` the board
// stops running it (see firmware/src/main.cpp) and an upload replaces it, so a
// rig can never quietly run the demo believing it is running an experiment.
#pragma once
#include <cstdint>

#include "config.h"
#include "graph/state_graph.h"

namespace statemachined {
namespace demo {

/// The lines the demo uses, which is what you wire.
///
/// Input line numbers, not pins: `dev/HARDWARE.md` maps line to pin per board,
/// and on the Uno R4 Minima inputs 0 and 1 are D2 and D3, outputs 0..4 are
/// D10, D11, D12, A0, A1, and output 7 is A4.
enum : LineIndex {
  kStartInput = 0,  ///< push to start a trial. Active high, 20 ms debounce
  kAbortInput = 1,  ///< push mid-trial to abort it
};
enum : LineIndex {
  kFirstStepOutput = 0,  ///< outputs 0..4: one LED per step, lit in turn
  kStepCount = 5,
  kReadyOutput = 7,  ///< lit while the graph is waiting for the start switch
};

/// How long each step holds its LED. Fixed rather than drawn: the point is for
/// somebody to be able to count along with it.
constexpr Milliseconds kStepMs = 500;

/// Fill `g` with the demo graph. Deterministic, allocation-free, and always
/// passes validate() -- the test asserts that, so a change that breaks it fails
/// on the host rather than on a bench with a board in hand.
///
/// The shape:
///
///     wait  --(start switch rises)-->  step0 -500ms-> ... -500ms-> step4
///                                        |                           |
///                                   (abort rises)                 500 ms
///                                        v                           v
///                                     aborted                       done
///
/// `wait` has no timeout, so the board sits there with the ready lamp lit until
/// somebody presses the switch. Each step raises its own LED on entry and the
/// machine lowers it on exit, so what you see is one light walking across five
/// pins. `done` is terminal and reports Hit; `aborted` is terminal and reports
/// Cancelled. A terminal state's entry actions run and nothing exits it, so its
/// LED stays lit as a result lamp until the next trial starts.
void build(StateGraph& g);

}  // namespace demo
}  // namespace statemachined
