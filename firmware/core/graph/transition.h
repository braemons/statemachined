// SPDX-License-Identifier: GPL-3.0-or-later
// A trigger is a predicate over the whole input word, not an edge on one line.
//
// Three bitmasks over the input lines, evaluated in constant time with no
// allocation. Each names one clause of the guard:
//
//     (w & all_high) == all_high                    every one of these is high
//  && (any_high == 0 || (w & any_high) != 0)        at least one of these is high
//  && (w & none_high) == 0                          none of these is high
//
// The transition fires on the predicate's own **false->true edge**, not on its
// level. That one rule subsumes everything: a rising edge on one line is
// all_high={line}; a falling edge is none_high={line}; "both levers at once" is
// all_high={l,r} and fires on the transition into that combination however the
// animal gets there; "responded while not holding" is all_high plus none_high
// together.
//
// Bpod's Condition is one channel and one value, so combinations are not
// expressible there. This is the whole reason for the mask design. The type is
// called Transition rather than Condition because it carries a target state --
// it is a guarded transition, not a predicate -- and because "condition" in a
// neuroscience codebase means the experimental condition, which is exactly the
// thing this firmware must never learn.
//
// See dev/PLAN.md, "Triggers, including combinations of TTL lines".
#pragma once
#include <cstdint>

#include "config.h"

namespace statemachined {

struct Transition {
  LineBitmask all_high = 0;   ///< every one of these lines must be high
  LineBitmask any_high = 0;   ///< at least one must be high; 0 means "don't care"
  LineBitmask none_high = 0;  ///< none of these lines may be high

  StateIndex target_state = 0;  ///< where this transition goes

  /// Which distribution the required hold is drawn from, once, on state entry.
  /// The predicate must then stay true that long before the transition fires,
  /// which is what makes "both levers held for 200 ms" one guard rather than
  /// hand-rolled bookkeeping. Drawn rather than fixed so a hold can be
  /// randomised per trial, and reported afterwards so the draw is evidence.
  RandomDistributionIndex hold_duration = kNoRandomDistribution;

  /// Fire immediately if the predicate already holds when the state is entered,
  /// instead of waiting for a false->true edge. The default is the edge, which
  /// is "wait for the press" rather than "wait until held" -- a lever the animal
  /// is already holding must not end the trial instantly.
  bool fire_if_true_on_entry = false;

  /// The predicate alone, with no edge or hold logic.
  constexpr bool holds(LineBitmask w) const {
    return (w & all_high) == all_high && (any_high == 0 || (w & any_high) != 0) &&
           (w & none_high) == 0;
  }
};

/// Per-transition runtime state: the edge latch and the hold timer.
struct TransitionState {
  bool was_true = false;
  bool armed = false;               ///< false until the predicate has been false once, unless
                                    ///< the transition is fire_if_true_on_entry
  Microseconds held_since = 0;      ///< micros when the predicate last became true
  Milliseconds hold_needed_ms = 0;  ///< drawn on state entry
};

}  // namespace statemachined
