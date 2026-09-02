// A trigger is a predicate over the whole input word, not an edge on one line.
//
// Three masks, evaluated in constant time with no allocation:
//
//     (w & all)  == all
//  && (any == 0 || (w & any) != 0)
//  && (w & none) == 0
//
// The transition fires on the predicate's own **false->true edge**, not on its
// level. That one rule subsumes everything: a rising edge on one line is
// all={line}; a falling edge is none={line}; "both levers at once" is
// all={l,r} and fires on the transition into that combination however the
// animal gets there; "responded while not holding" is all plus none together.
//
// Bpod's Condition is one channel and one value, so combinations are not
// expressible there. This is the whole reason for the mask design.
//
// See dev/PLAN.md, "Triggers, including combinations of TTL lines".
#pragma once
#include <cstdint>

namespace fsmd {

struct Condition {
  uint32_t all = 0;
  uint32_t any = 0;
  uint32_t none = 0;
  uint8_t goto_state = 0;
  uint8_t hold_dist = 0xFF;  ///< index into the dist pool; 0xFF = no hold
  bool level = false;        ///< fire even if already true on state entry;
                             ///< default is edge semantics ("wait for the
                             ///< press", not "wait until held")

  /// The predicate alone, with no edge or hold logic.
  constexpr bool holds(uint32_t w) const {
    return (w & all) == all && (any == 0 || (w & any) != 0) && (w & none) == 0;
  }
};

/// Per-condition runtime state: the edge latch and the hold timer.
struct ConditionState {
  bool was_true = false;
  bool armed = false;          ///< false until the predicate has been false once,
                               ///< unless the condition is `level`
  uint32_t held_since = 0;     ///< micros when the predicate last became true
  int32_t hold_needed_ms = 0;  ///< drawn on state entry
};

}  // namespace fsmd
