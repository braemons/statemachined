// SPDX-License-Identifier: GPL-3.0-or-later
#include "machine/state_machine.h"

namespace statemachined {
namespace {
/// Unsigned wraparound-safe elapsed time. micros() wraps every ~71 minutes on a
/// 32-bit counter and a trial must not care.
inline uint32_t since(uint32_t from, uint32_t now) { return now - from; }

/// Has `deadline` passed? Signed difference, so it stays right across the
/// ~71-minute wrap of a 32-bit microsecond counter: correct as long as the
/// deadline is within ~35 minutes of now, and a pulse width is milliseconds.
inline bool reached(uint32_t deadline, uint32_t now) {
  return static_cast<int32_t>(now - deadline) >= 0;
}

/// Merge `later` over `earlier`, per line. Not two ORs: within one scan a line
/// can be lowered by a state's exit and raised again by the next state's entry,
/// and the answer is "high", not "both".
inline void merge(OutputUpdate& earlier, const OutputUpdate& later) {
  earlier.set_high = (earlier.set_high | later.set_high) & ~later.set_low;
  earlier.set_low = (earlier.set_low | later.set_low) & ~later.set_high;
}
}  // namespace

void StateMachine::note(const OutputUpdate& ops) {
  driven_ = (driven_ | ops.set_high) & ~ops.set_low;
  pulsing_ &= ~ops.set_low;  // a line driven low is no longer pulsing
  raised_ &= ~ops.set_low;
}

OutputUpdate StateMachine::start(uint64_t seed, Microseconds now_us, LineBitmask word) {
  // Anything the previous run left raised comes down here. A terminal state's
  // entry actions run but nothing can lower them -- there is no exit from a
  // terminal state -- so this is where that debt is paid. It means a line left
  // high by the last trial cannot survive into the next one unnoticed, which is
  // the same guarantee leave() makes within a run.
  const LineBitmask left_over = raised_;

  record_ = StateMachineRunRecord{};
  rng_.reseed(seed);
  running_ = true;
  started_us_ = now_us;
  raised_ = 0;
  have_last_word_ = false;
  hold_pending_ = false;
  has_pending_ = false;
  pending_ = OutputUpdate{};

  enter(graph().entry, now_us, word);
  const State& s = set_->states[current_];
  OutputUpdate ops = apply_actions(s.first_entry_action, s.entry_action_count, now_us);
  ops.set_low |= left_over & ~ops.set_high;
  raised_ |= ops.set_high;
  note(ops);
  return ops;
}

void StateMachine::enter(StateIndex state, Microseconds now_us, LineBitmask word) {
  current_ = state;
  entered_us_ = now_us;
  // This state's transitions have never been looked at. See advance().
  just_entered_ = true;
  const State& s = set_->states[state];

  timeout_ms_ = (s.timeout_duration == kNoRandomDistribution)
                    ? -1
                    : set_->distributions[s.timeout_duration].draw(rng_);

  // Arm this state's transitions against the input word AS IT IS AT ENTRY.
  //
  // A transition is edge-triggered by default: it may not fire on a predicate
  // that was *already* true when the state was entered -- "wait for the press",
  // not "wait until held" -- and `level` opts out of that. Arming has to be
  // decided against the real word, not assumed: a line that rises between entry
  // and the first scan is a genuine rising edge and must fire.
  for (uint8_t c = 0; c < s.transition_count; ++c) {
    const TransitionIndex i = s.first_transition + c;
    const Transition& cond = set_->transitions[i];
    TransitionState& cs = trans_state_[i];
    cs.was_true = cond.holds(word);
    cs.armed = cond.fire_if_true_on_entry || !cs.was_true;
    cs.held_since = now_us;
    const RandomDistributionIndex hd = set_->transitions[i].hold_duration;
    cs.hold_needed_ms = (hd == kNoRandomDistribution) ? 0 : set_->distributions[hd].draw(rng_);
  }
}

OutputUpdate StateMachine::apply_actions(OutputActionIndex first, uint8_t count,
                                         Microseconds now_us) {
  OutputUpdate ops;
  // The running level, seeded from the shadow. A list saying `toggle` twice on
  // one line means "back where it started", and the second toggle has to see
  // what the first one did rather than what the pin was before either.
  LineBitmask level = driven_;
  for (uint8_t i = 0; i < count; ++i) {
    const OutputAction& a = set_->output_actions[first + i];
    const LineBitmask bit = 1u << a.output_line;
    bool high;
    switch (a.kind) {
      case OutputActionKind::High:
        high = true;
        break;
      case OutputActionKind::Low:
        high = false;
        break;
      case OutputActionKind::Toggle:
        high = (level & bit) == 0;
        break;
      case OutputActionKind::Pulse:
      default:
        high = true;
        // Re-pulsing a line that is already pulsing restarts it at the new
        // width -- which is what a graph re-entering a state means by it.
        pulsing_ |= bit;
        pulse_deadline_us_[a.output_line] =
            now_us + static_cast<Microseconds>(a.pulse_ms) * 1000u;
        break;
    }
    if (high) {
      ops.set_high |= bit;
      ops.set_low &= ~bit;
      level |= bit;
    } else {
      ops.set_low |= bit;
      ops.set_high &= ~bit;
      level &= ~bit;
      pulsing_ &= ~bit;
    }
  }
  driven_ = level;
  return ops;
}

void StateMachine::record_visit(StateExitCause cause, TransitionIndex fired,
                                Microseconds now_us) {
  StateVisit e;
  // Within this graph, not into the shared pool: the host resolves it against
  // the graph it selected.
  e.state_index = static_cast<StateIndex>(current_ - graph().first_state);
  e.cause = cause;
  e.transition_index = fired;
  e.drawn_ms = timeout_ms_;
  e.entered_us = entered_us_;
  e.duration_us = since(entered_us_, now_us);

  // A ring, and it drops from the front. A graph may loop, and a long trial has
  // to degrade to a truncated path rather than to a corrupt one -- but which
  // half survives is a choice, and the end of a trial is where the response is.
  // Keeping the first 255 visits and discarding everything after was the wrong
  // half. `truncated` still says it happened, and total_visits says by how much.
  const uint16_t next = static_cast<uint16_t>(record_.path_first) + record_.path_len;
  const uint8_t slot = static_cast<uint8_t>(next < kMaxPath ? next : next - kMaxPath);
  record_.path[slot] = e;
  if (record_.path_len < kMaxPath) {
    ++record_.path_len;
  } else {
    record_.path_first =
        static_cast<uint8_t>(record_.path_first + 1 < kMaxPath ? record_.path_first + 1 : 0);
    record_.path_truncated = true;
  }

  // After the record, never before: a sink that took a long time must not be
  // able to leave the run's own account of itself half written. The seq is the
  // visit's ordinal in the run, so a gap in the stream is detectable.
  const uint32_t seq = record_.total_visits++;
  if (visits_ != nullptr) visits_->on_visit(e, seq);
}

OutputUpdate StateMachine::leave(StateExitCause cause, TransitionIndex fired,
                                 Microseconds now_us) {
  record_visit(cause, fired, now_us);
  const State& s = set_->states[current_];
  OutputUpdate ops = apply_actions(s.first_exit_action, s.exit_action_count, now_us);
  // Everything this state raised comes down, whether or not the graph said so.
  // A valve left open because a graph forgot an on_exit is not an acceptable
  // failure mode, so the machine guarantees it rather than trusting the data.
  ops.set_low |= raised_ & ~ops.set_high;
  raised_ = 0;
  return ops;
}

bool StateMachine::force_end(Microseconds now_us) {
  if (!running_) return false;  // first terminal decision wins
  OutputUpdate ops = leave(StateExitCause::Cancel, kNoTransition, now_us);
  record_.force_ended = true;
  record_.total_us = since(started_us_, now_us);
  running_ = false;
  pending_ = ops;
  has_pending_ = true;
  return true;
}

OutputUpdate StateMachine::service_outputs(Microseconds now_us) {
  OutputUpdate ops;
  if (has_pending_) {  // outputs owed by a cancel that happened between scans
    ops = pending_;
    has_pending_ = false;
    pending_ = OutputUpdate{};
  }

  // The common scan has no pulse in flight and pays one compare for it.
  for (LineBitmask rest = pulsing_; rest != 0;) {
    const uint8_t line = static_cast<uint8_t>(__builtin_ctz(rest));
    const LineBitmask bit = 1u << line;
    rest &= ~bit;
    if (reached(pulse_deadline_us_[line], now_us)) {
      ops.set_low |= bit;
      ops.set_high &= ~bit;
    }
  }

  note(ops);
  return ops;
}

OutputUpdate StateMachine::advance(LineBitmask word, Microseconds now_us) {
  // Pulses and cancel debts come down whether or not a run is in flight, so
  // this is one call rather than a copy of the same logic.
  OutputUpdate ops = service_outputs(now_us);
  if (!running_) return ops;

  // The runtime cap. Validation proves a terminal state is reachable; it cannot
  // prove one is reached.
  if (run_cap_ms_ > 0 &&
      since(started_us_, now_us) >= static_cast<uint32_t>(run_cap_ms_) * 1000u) {
    force_end(now_us);
    record_.hit_run_cap = true;
    merge(ops, service_outputs(now_us));
    return ops;
  }

  const State& s = set_->states[current_];
  StateIndex next = kNoState;
  TransitionIndex fired = kNoTransition;
  StateExitCause cause = StateExitCause::Timeout;

  // Skip predicate evaluation entirely when nothing on the inputs moved and no
  // transition is mid-hold. At 10 kHz the overwhelmingly common scan then costs
  // one compare. A transition accumulating a hold must still be re-checked, or
  // "both levers held for 200 ms" would never fire on a steady word.
  //
  // Entering a state is itself an event, and `just_entered_` is why. The word
  // is machine-wide, not per-state, so a transition into a state on a steady
  // input word left the new state's transitions unevaluated until something on
  // the inputs happened to move. Edge-triggered transitions never noticed --
  // they need a change by definition -- but a `level` transition whose
  // predicate is already true at entry has no edge coming, and silently never
  // fired. That is exactly the case `level` exists for: "wait until held",
  // where the lever is already down and nothing is going to move. Found on
  // hardware, by daemon/tests/hardware/test_lines.py, because every host
  // test for `level` entered its state at start() -- where have_last_word_ is
  // false and the first scan therefore evaluated anyway.
  const bool word_changed = !have_last_word_ || word != last_word_;
  last_word_ = word;
  have_last_word_ = true;
  const bool need_eval = word_changed || hold_pending_ || just_entered_;
  hold_pending_ = false;
  just_entered_ = false;

  // Only this state's transitions are evaluated -- cost is bounded by
  // transitions-per-state, never by graph size. Declaration order resolves ties:
  // the first transition in the list wins, as in Bpod.
  for (uint8_t c = 0; need_eval && c < s.transition_count && next == kNoState; ++c) {
    const TransitionIndex i = s.first_transition + c;
    const Transition& cond = set_->transitions[i];
    TransitionState& cs = trans_state_[i];
    const bool now_true = cond.holds(word);

    if (!now_true) {
      cs.armed = true;  // seen false: an edge transition may now fire
      cs.was_true = false;
      continue;
    }
    if (!cs.was_true) {
      cs.was_true = true;
      cs.held_since = now_us;  // the hold clock starts at the rising edge
    }
    if (!cs.armed) continue;  // predicate was already true at state entry
    if (cs.hold_needed_ms > 0 &&
        since(cs.held_since, now_us) < static_cast<uint32_t>(cs.hold_needed_ms) * 1000u) {
      hold_pending_ = true;  // re-check next scan even on an unchanged word
      continue;
    }
    next = cond.target_state;
    fired = c;
    cause = StateExitCause::Transition;
  }

  if (next == kNoState && timeout_ms_ >= 0 &&
      since(entered_us_, now_us) >= static_cast<uint32_t>(timeout_ms_) * 1000u) {
    next = s.timeout_target;
    cause = StateExitCause::Timeout;
  }

  if (next == kNoState) return ops;

  // A self-transition is a real transition: it re-runs exit and entry output_actions,
  // resets the timer and REDRAWS the random duration. Bpod detects transitions
  // with `NewState != CurrentState`, which silently makes a self-loop a no-op;
  // a re-triggerable timeout should be expressible.
  const OutputUpdate exit_ops = leave(cause, fired, now_us);
  merge(ops, exit_ops);

  const State& target = set_->states[next];
  if (target.terminal()) {
    record_.terminal_code = target.terminal_code;
    record_.total_us = since(started_us_, now_us);
    current_ = next;
    record_visit(StateExitCause::Terminal, kNoTransition, now_us);
    running_ = false;

    // A terminal state's entry actions DO run: "pulse the valve on entering
    // Hit" is how a reward is written, and the alternative -- making authors
    // hang it off the exit of whichever state happened to precede the terminal
    // one -- spreads one intention over every route into it.
    //
    // What the machine cannot do is lower them by *exiting*, because there is
    // no exit from a terminal state. So a Pulse still comes down on its own
    // width -- service_outputs() keeps ticking after the run has ended, which
    // is exactly why it is separate from advance() -- and anything set High
    // stays high until the next trial begins or fail_safe runs. High is left
    // raised, carried in raised_, and the next start() pays it off.
    //
    // Which is the argument for writing a reward as `pulse` rather than as
    // `high`: only one of the two comes down on its own.
    const OutputUpdate final_ops =
        apply_actions(target.first_entry_action, target.entry_action_count, now_us);
    merge(ops, final_ops);
    raised_ = final_ops.set_high;
    note(ops);
    return ops;
  }

  enter(next, now_us, word);
  const OutputUpdate entry_ops =
      apply_actions(target.first_entry_action, target.entry_action_count, now_us);
  merge(ops, entry_ops);
  raised_ |= entry_ops.set_high;
  note(ops);
  return ops;
}

}  // namespace statemachined
