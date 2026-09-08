// SPDX-License-Identifier: GPL-3.0-or-later
// The trial layer's vocabulary. None of this is known to the state machine.
//
// statemachined is the *timing* authority and triald is the *decision* authority: what
// crosses the wire is a report of what happened, not a verdict on it. The
// firmware never learns the trial type -- it receives a graph, timings and a
// reward duration, which is what keeps firmware stable while paradigms change.
#pragma once
#include <cstdint>

#include "config.h"
#include "graph/state.h"
#include "machine/state_machine.h"

namespace statemachined {

/// triald's .tdr outcome codes. A wire contract: these values are in every .tdr
/// the lab has written and every analysis script that reads one. NEVER
/// renumber. Mirrors triald's TrialOutcome and VStim's TDR::TrialOutcome.
enum class TrialOutcome : int8_t {
  Undetermined = -1,
  NotStarted = 0,
  Hit = 1,
  WrongResponse = 2,
  EarlyHit = 3,
  EarlyWrongResponse = 4,
  Early = 5,
  Late = 6,
  EyeError = 7,
  UnexpectedStartSignal = 8,
  WrongStartSignal = 9,
  Cancelled = 10,
};

/// The values line up by construction, so a terminal state stores a TrialOutcome
/// without the graph or the machine having to know that is what it is.
constexpr TerminalCode terminal_code_of(TrialOutcome o) { return static_cast<TerminalCode>(o); }
constexpr TrialOutcome outcome_of(TerminalCode c) { return static_cast<TrialOutcome>(c); }

enum class TrialCancelReason : uint8_t {
  None = 0,
  Host = 1,          ///< the experimenter, via triald and the bridge
  LinkLost = 2,      ///< heartbeat gap
  AbortLine = 3,     ///< the always-live hardware abort input
  TrialTimeout = 4,  ///< the wall-clock cap on total trial duration
};

/// What statemachined adds to a run to make it a trial: an identity and a verdict on how
/// it ended. Deliberately does NOT embed the StateMachineRunRecord. That record holds
/// kMaxPath StateVisits -- about 1 KB on the reference board -- and copying it
/// here would double the largest buffer in the system on a part with 32 KB.
/// TrialRunner exposes the two side by side instead.
struct TrialRecord {
  uint32_t trial_id = 0;
  TrialOutcome outcome = TrialOutcome::Undetermined;
  TrialCancelReason cancel_reason = TrialCancelReason::None;
};

}  // namespace statemachined
