// SPDX-License-Identifier: GPL-3.0-or-later
// Raw pins in, one clean input word out: polarity normalised, disabled lines
// zeroed, every line debounced.
//
// This is portable logic, not HAL logic, and the distinction is the point. A
// debounce implemented per board is a debounce nothing on the host can test and
// that four HALs get subtly differently -- and it is exactly the kind of timing
// a rig blames on the animal. The HAL is left reading a port register.
//
// Debounce is per line rather than per condition because a predicate over
// several lines is what chatters worst: two lines each bouncing once can make a
// combination predicate rise and fall several times, and no amount of care in
// the graph can filter that after the fact.
#pragma once
#include <cstdint>

#include "config.h"
#include "io/wiring.h"

namespace statemachined {

class InputConditioner {
 public:
  /// Config arrives with the graph and can change when a new one is committed.
  /// Held by value: a graph is replaced wholesale and a dangling reference into
  /// the old one would be read every scan.
  void configure(const InputConfig& cfg) { cfg_ = cfg; }

  /// One scan. `raw` is the port as read, before any interpretation. Returns
  /// the word the state machine sees.
  ///
  /// A line must hold its new level continuously for its debounce time before
  /// the change is accepted; a line that falls back before then is a bounce and
  /// the wait is abandoned. A debounce of 0 -- the default -- accepts on the
  /// same scan, so this costs an xor and a compare on a quiet rig.
  LineBitmask apply(LineBitmask raw, Microseconds now_us);

  /// Adopt `raw` as the accepted level with no debounce wait. For the first
  /// scan after a reset or a new graph, where there is no previous level for a
  /// change to be measured against and every line would otherwise look like it
  /// had just moved.
  void prime(LineBitmask raw);

  /// What the last apply() returned.
  LineBitmask word() const { return stable_; }

  /// Lines whose raw level currently disagrees with the accepted one and whose
  /// debounce has not yet elapsed. Diagnostic: a line permanently in here is a
  /// line bouncing faster than its debounce, which is a wiring fault.
  LineBitmask settling() const { return pending_; }

 private:
  InputConfig cfg_;
  LineBitmask stable_ = 0;   ///< the accepted, conditioned word
  LineBitmask pending_ = 0;  ///< lines mid-debounce
  Microseconds since_us_[kMaxLines] = {0};
};

}  // namespace statemachined
