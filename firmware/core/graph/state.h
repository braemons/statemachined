// A state, and why one gets left.
//
// A state knows nothing about trials. Terminal states carry an opaque
// TerminalCode which the state machine reports back untouched; giving that code
// a meaning is the trial layer's job (see trial.h). That is what lets the
// machine be tested, and reused, without any of the outcome vocabulary.
#pragma once
#include <cstdint>

#include "config.h"

namespace fsmd {

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

  constexpr bool terminal() const { return terminal_code != kNotTerminal; }
};

}  // namespace fsmd
