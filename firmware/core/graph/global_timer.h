// SPDX-License-Identifier: GPL-3.0-or-later
// A timer that runs beside the state machine rather than inside it.
//
// **A running timer is an input line that is high.** Everything else here is
// detail. A timer holds `timer_line(i)` high while it runs, and a Transition
// waits on that bit exactly as it waits on a lever -- so "when the foreperiod
// timer ends" is `none_high={that line}` and needs no new vocabulary, no event
// codes, and no second matching path beside the predicate in transition.h.
//
// That is VStim's design, not an invention: its `TimerQueue` of sixteen timers
// (VStimLib/Timer/) reads an input virtual trigger line and writes an output
// one, in the same flat 68-line space its intervals transition on
// (VStimLib/Shared/Constants.h -- interval markers, system events, free lines
// and the real levers, all one kind of thing). Bpod took the other road:
// dedicated event codes (`GlobalTimer1_Start`, `GlobalTimer1_End`) and
// dedicated `GlobalTimerStartMatrix` / `GlobalTimerEndMatrix` beside the input
// matrix. That is the same feature bought twice, and it is part of why Bpod
// needs a Due. See dev/PLAN.md open question 3.
//
// What one timer can express, against the two references:
//
//   VStim DelayPulse       delay set, width set, loops = 1
//   VStim FrequencyGen     width and gap set, loops = 0 (forever)
//   Bpod  OnsetDelay       delay
//   Bpod  Loop/LoopInterval  loops / gap
//   Valve.cpp's VOT split  loops = n, width = open, gap = closed
//
// VStim's third type, FrequencyDivider, is deliberately absent: it counts input
// pulses, and counting is what dev/PLAN.md open question 3 assigns to triald,
// which owns the trial-to-trial bookkeeping a counter is for.
#pragma once
#include <cstdint>

#include "config.h"

namespace statemachined {

/// One global timer, as a set declares it.
///
/// Every duration is an index into the set's shared distribution pool, like a
/// timeout or a hold, rather than a number stored inline: a timer's delay is
/// exactly the kind of thing a paradigm wants jittered per trial, and drawing
/// it from the pool means it replays from the seed and is reported as evidence
/// instead of merely being reproducible. VStim quantises all three to the video
/// frame (`RuntimeUnitConverter::NoOfFrames`), because its clock is the
/// vertical retrace; ours is a 10 kHz scan and needs no such rounding.
struct GlobalTimer {
  /// What starts it: the same three masks as a Transition, evaluated by the
  /// same `holds()` and fired on the same false->true edge.
  ///
  /// A predicate rather than VStim's single line plus flank, because the
  /// predicate already exists and is strictly more general -- and because the
  /// timers' own bits are in this word, so a timer can trigger another timer.
  /// That is Bpod's `OnsetTrigger` and VStim's divider chain, arrived at by not
  /// writing anything.
  ///
  /// All three zero means the predicate is vacuously true, which would fire on
  /// the first scan; the builder refuses that, so a timer with no trigger masks
  /// is one started only by a TimerStart action.
  LineBitmask all_high = 0;
  LineBitmask any_high = 0;
  LineBitmask none_high = 0;

  /// Drawn when the timer is triggered: how long before it goes high. Absent
  /// means it goes high on the triggering scan.
  RandomDistributionIndex delay = kNoRandomDistribution;

  /// Drawn per pulse: how long it stays high. Required -- a timer with no width
  /// is a timer that is never high, which is nothing.
  RandomDistributionIndex width = kNoRandomDistribution;

  /// Drawn per pulse: the dead time before the next one, when `loops` asks for
  /// another. A gap rather than a period, so there is no way to write a period
  /// shorter than its own pulse. Bpod's `LoopInterval`, and the closed half of
  /// Valve.cpp's split reward.
  RandomDistributionIndex gap = kNoRandomDistribution;

  /// A real output line driven high alongside the timer's own bit, or kNoLine.
  /// Optional because a timer that only gates a transition needs no pin.
  LineIndex output_line = kNoLine;

  /// How many pulses one trigger produces. 1 is VStim's DelayPulse, 0 is its
  /// FrequencyGenerator -- forever, until cancelled or the session resets.
  uint8_t loops = 1;

  /// Drive `output_line` LOW while the timer runs, rather than high. VStim's
  /// `m_PulsePolarity`, and the same fact `wiring`'s safe levels exist for: on
  /// a rig with an active-low driver, "on" is not "high". The timer's own *bit*
  /// is unaffected -- it is high while running whatever the pin does, because a
  /// predicate reading it should not have to know how the valve is wired.
  bool active_low = false;

  /// Reset when a run ends, rather than being left to finish.
  ///
  /// VStim decides this per timer too (`m_TrialBound`, set when the timer names
  /// stimulus numbers): `StartTimer()` at trial start, `ResetTimer()` at trial
  /// end, which also kills a pulse in flight. False -- the default -- is the
  /// free-running case, which is the one that lets a timer raise a line while
  /// the device sits between trials.
  bool trial_bound = false;
};

}  // namespace statemachined
