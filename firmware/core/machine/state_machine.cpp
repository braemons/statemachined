#include "machine/state_machine.h"

namespace fsmd {
namespace {
/// Unsigned wraparound-safe elapsed time. micros() wraps every ~71 minutes on a
/// 32-bit counter and a trial must not care.
inline uint32_t since(uint32_t from, uint32_t now) { return now - from; }
}  // namespace

OutputUpdate StateMachine::start(uint64_t seed, Microseconds now_us, LineBitmask word) {
  record_ = StateMachineRunRecord{};
  rng_.reseed(seed);
  running_ = true;
  started_us_ = now_us;
  raised_ = 0;
  have_last_word_ = false;
  hold_pending_ = false;
  has_pending_ = false;
  pending_ = OutputUpdate{};

  enter(graph_->entry, now_us, word);
  const State& s = graph_->states[current_];
  OutputUpdate ops = apply_actions(s.first_entry_action, s.entry_action_count);
  raised_ |= ops.set_high;
  return ops;
}

void StateMachine::enter(StateIndex state, Microseconds now_us, LineBitmask word) {
  current_ = state;
  entered_us_ = now_us;
  const State& s = graph_->states[state];

  timeout_ms_ = (s.timeout_duration == kNoRandomDistribution)
                    ? -1
                    : graph_->distributions[s.timeout_duration].draw(rng_);

  // Arm this state's transitions against the input word AS IT IS AT ENTRY.
  //
  // A transition is edge-triggered by default: it may not fire on a predicate
  // that was *already* true when the state was entered -- "wait for the press",
  // not "wait until held" -- and `level` opts out of that. Arming has to be
  // decided against the real word, not assumed: a line that rises between entry
  // and the first scan is a genuine rising edge and must fire.
  for (uint8_t c = 0; c < s.transition_count; ++c) {
    const TransitionIndex i = s.first_transition + c;
    const Transition& cond = graph_->transitions[i];
    TransitionState& cs = trans_state_[i];
    cs.was_true = cond.holds(word);
    cs.armed = cond.fire_if_true_on_entry || !cs.was_true;
    cs.held_since = now_us;
    const RandomDistributionIndex hd = graph_->transitions[i].hold_duration;
    cs.hold_needed_ms =
        (hd == kNoRandomDistribution) ? 0 : graph_->distributions[hd].draw(rng_);
  }
}

OutputUpdate StateMachine::apply_actions(OutputActionIndex first, uint8_t count) const {
  OutputUpdate ops;
  for (uint8_t i = 0; i < count; ++i) {
    const OutputAction& a = graph_->output_actions[first + i];
    const LineBitmask bit = 1u << a.output_line;
    switch (a.kind) {
      case OutputActionKind::High:
      case OutputActionKind::Pulse:  // the pulse's falling edge is the HAL's timer
        ops.set_high |= bit;
        break;
      case OutputActionKind::Low:
        ops.set_low |= bit;
        break;
      case OutputActionKind::Toggle:
        // Toggle is resolved against the live level by the HAL; represent it as
        // neither, and let the HAL read-modify-write.
        break;
    }
  }
  return ops;
}

void StateMachine::record_visit(StateExitCause cause, TransitionIndex fired,
                                Microseconds now_us) {
  if (record_.path_len < kMaxPath) {
    StateVisit& e = record_.path[record_.path_len++];
    e.state_index = current_;
    e.cause = cause;
    e.transition_index = fired;
    e.drawn_ms = timeout_ms_;
    e.entered_us = entered_us_;
    e.duration_us = since(entered_us_, now_us);
  } else {
    // A graph may loop. Truncate the path rather than corrupt it, and say so.
    record_.path_truncated = true;
  }
}

OutputUpdate StateMachine::leave(StateExitCause cause, TransitionIndex fired,
                                 Microseconds now_us) {
  record_visit(cause, fired, now_us);
  const State& s = graph_->states[current_];
  OutputUpdate ops = apply_actions(s.first_exit_action, s.exit_action_count);
  // Everything this state raised comes down, whether or not the graph said so.
  // A valve left open because a graph forgot an on_exit is not an acceptable
  // failure mode, so the machine guarantees it rather than trusting the data.
  ops.set_low |= raised_ & ~ops.set_high;
  raised_ = 0;
  return ops;
}

bool StateMachine::halt(Microseconds now_us) {
  if (!running_) return false;  // first terminal decision wins
  OutputUpdate ops = leave(StateExitCause::Cancel, kNoTransition, now_us);
  record_.halted = true;
  record_.total_us = since(started_us_, now_us);
  running_ = false;
  pending_ = ops;
  has_pending_ = true;
  return true;
}

OutputUpdate StateMachine::scan(LineBitmask word, Microseconds now_us) {
  OutputUpdate ops;
  if (has_pending_) {  // outputs owed by a cancel that happened between scans
    ops = pending_;
    has_pending_ = false;
    pending_ = OutputUpdate{};
  }
  if (!running_) return ops;

  // The runtime cap. Validation proves a terminal state is reachable; it cannot
  // prove one is reached.
  if (run_cap_ms_ > 0 &&
      since(started_us_, now_us) >= static_cast<uint32_t>(run_cap_ms_) * 1000u) {
    halt(now_us);
    record_.hit_run_cap = true;
    if (has_pending_) {
      ops.set_high |= pending_.set_high;
      ops.set_low |= pending_.set_low;
      has_pending_ = false;
      pending_ = OutputUpdate{};
    }
    return ops;
  }

  const State& s = graph_->states[current_];
  StateIndex next = kNoState;
  TransitionIndex fired = kNoTransition;
  StateExitCause cause = StateExitCause::Timeout;

  // Skip predicate evaluation entirely when nothing on the inputs moved and no
  // transition is mid-hold. At 10 kHz the overwhelmingly common scan then costs
  // one compare. A transition accumulating a hold must still be re-checked, or
  // "both levers held for 200 ms" would never fire on a steady word.
  const bool word_changed = !have_last_word_ || word != last_word_;
  last_word_ = word;
  have_last_word_ = true;
  const bool need_eval = word_changed || hold_pending_;
  hold_pending_ = false;

  // Only this state's transitions are evaluated -- cost is bounded by
  // transitions-per-state, never by graph size. Declaration order resolves ties:
  // the first transition in the list wins, as in Bpod.
  for (uint8_t c = 0; need_eval && c < s.transition_count && next == kNoState; ++c) {
    const TransitionIndex i = s.first_transition + c;
    const Transition& cond = graph_->transitions[i];
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
  ops.set_high |= exit_ops.set_high;
  ops.set_low |= exit_ops.set_low;

  const State& target = graph_->states[next];
  if (target.terminal()) {
    record_.terminal_code = target.terminal_code;
    record_.total_us = since(started_us_, now_us);
    current_ = next;
    record_visit(StateExitCause::Terminal, kNoTransition, now_us);
    running_ = false;
    return ops;
  }

  enter(next, now_us, word);
  const OutputUpdate entry_ops =
      apply_actions(target.first_entry_action, target.entry_action_count);
  ops.set_high |= entry_ops.set_high;
  ops.set_low = (ops.set_low | entry_ops.set_low) & ~entry_ops.set_high;
  raised_ |= entry_ops.set_high;
  return ops;
}

}  // namespace fsmd
