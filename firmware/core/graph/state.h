// SPDX-License-Identifier: GPL-3.0-or-later
// A state, and why one gets left.
//
// A state knows nothing about trials. Terminal states carry an opaque
// TerminalCode which the state machine reports back untouched; giving that code
// a meaning is the trial layer's job (see trial.h). That is what lets the
// machine be tested, and reused, without any of the outcome vocabulary.
#pragma once
#include <cstdint>

#include "config.h"

namespace statemachined {

/// Why a state was left. Note this is per *state*, not per trial: Terminal is
/// simply the cause that happens to also end the run.
enum class StateExitCause : uint8_t {
  Timeout = 0,
  Transition = 1,
  Cancel = 2,
  Terminal = 3,  ///< the run ended here
};

/// What a terminal state reports. Opaque to the machine, which only ever
/// compares it against kNotTerminal and copies it into the record. The trial
/// layer maps it onto a TrialOutcome.
using TerminalCode = int8_t;
constexpr TerminalCode kNotTerminal = -1;

/// A dwell that was never drawn, because the terminal state reached declares
/// none. Signed like every other Milliseconds for the same reason: a dwell of
/// zero is a legal instruction -- "start the next run on the next scan" -- and
/// must not read as the absence of one.
constexpr Milliseconds kNoRelight = -1;

/// A node of the graph. Its transitions and its output actions live in the
/// graph's shared pools rather than inside the state, so each is a (first,
/// count) run into the relevant array -- a slice, not a list. That is what makes
/// a 32-state graph fit in ~7.5 KB where Bpod's dense [state][event] matrix
/// needs a Due or a Teensy.
struct State {
  TransitionIndex first_transition = 0;  ///< slice of StateGraph::transitions
  uint8_t transition_count = 0;

  OutputActionIndex first_entry_action = 0;  ///< slice of output_actions, raised
  uint8_t entry_action_count = 0;            ///< on entry

  OutputActionIndex first_exit_action = 0;  ///< slice of output_actions, applied
  uint8_t exit_action_count = 0;            ///< on exit, beyond the automatic
                                            ///< lowering of everything raised

  /// Drawn once on entry; kNoRandomDistribution means the state has no timeout
  /// and can only be left through a transition or a cancel.
  RandomDistributionIndex timeout_duration = kNoRandomDistribution;
  StateIndex timeout_target = kNoState;  ///< where a timeout goes

  TerminalCode terminal_code = kNotTerminal;  ///< set => terminal

  /// How long a terminal state is dwelt in before the machine may be started
  /// again. kNoRandomDistribution means it may not be: reaching this terminal
  /// state is where a self-driving board stops.
  ///
  /// This does not give a terminal state an exit. Nothing exits a terminal
  /// state -- the run ends there and its record is closed before this is read
  /// -- and what the dwell decides is when the NEXT run may start. That is a
  /// fact about the paradigm rather than about the run, which is why it is
  /// written in the graph beside every other duration; whether anything acts on
  /// it is a property of the device (see HostLinkSession's autorun). A graph
  /// that declares one still runs unchanged under a host that arms every trial
  /// itself.
  ///
  /// Drawn from the shared pool like a timeout, so an inter-trial interval can
  /// be jittered, replays from the seed, and is reported as evidence rather
  /// than merely being reproducible.
  RandomDistributionIndex relight_duration = kNoRandomDistribution;

  constexpr bool terminal() const { return terminal_code != kNotTerminal; }
};

}  // namespace statemachined
