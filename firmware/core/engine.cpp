#include "engine.h"

namespace fsmd {
namespace {
/// Unsigned wraparound-safe elapsed time. micros() wraps every ~71 minutes on a
/// 32-bit counter and a trial must not care.
inline uint32_t since(uint32_t from, uint32_t now) { return now - from; }
}  // namespace

OutputUpdate TrialStateMachine::start(uint32_t trial_id, uint64_t session_seed,
                                      Microseconds now_us, LineBitmask word) {
  result_ = TrialRecord{};
  result_.trial_id = trial_id;
  rng_.reseed(Rng::mix(session_seed, trial_id));
  running_ = true;
  started_us_ = now_us;
  raised_ = 0;
  have_last_word_ = false;
  hold_pending_ = false;
  has_pending_ = false;
  pending_ = OutputUpdate{};
  if (graph_ == nullptr) return OutputUpdate{};

  enter(graph_->entry, now_us, word);
  const State& s = graph_->states[current_];
  OutputUpdate ops = apply_actions(s.entry_first, s.entry_count);
  raised_ |= ops.set_high;
  return ops;
}

void TrialStateMachine::enter(uint8_t state, Microseconds now_us, LineBitmask word) {
  current_ = state;
  entered_us_ = now_us;
  const State& s = graph_->states[state];

  timeout_ms_ = (s.timeout_duration == kNoDistribution)
                    ? -1
                    : graph_->distributions[s.timeout_duration].draw(rng_);

  // Arm this state's transitions against the input word AS IT IS AT ENTRY.
  //
  // A transition is edge-triggered by default: it may not fire on a predicate
  // that was *already* true when the state was entered -- "wait for the press",
  // not "wait until held" -- and `level` opts out of that. Arming has to be
  // decided against the real word, not assumed: a line that rises between entry
  // and the first scan is a genuine rising edge and must fire.
  for (uint8_t c = 0; c < s.trans_count; ++c) {
    const uint8_t i = s.trans_first + c;
    const Transition& cond = graph_->transitions[i];
    TransitionState& cs = trans_state_[i];
    cs.was_true = cond.holds(word);
    cs.armed = cond.fire_if_true_on_entry || !cs.was_true;
    cs.held_since = now_us;
    const uint8_t hd = graph_->transitions[i].hold_duration;
    cs.hold_needed_ms = (hd == kNoDistribution) ? 0 : graph_->distributions[hd].draw(rng_);
  }
}

OutputUpdate TrialStateMachine::apply_actions(uint8_t first, uint8_t count) const {
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

void TrialStateMachine::record(ExitCause cause, uint8_t trans_index, Microseconds now_us) {
  if (result_.path_len < kMaxPath) {
    StateVisit& e = result_.path[result_.path_len++];
    e.state_index = current_;
    e.cause = cause;
    e.transition_index = trans_index;
    e.drawn_ms = timeout_ms_;
    e.entered_us = entered_us_;
    e.duration_us = since(entered_us_, now_us);
  } else {
    // A graph may loop. Truncate the path rather than corrupt it, and say so.
    result_.path_truncated = true;
  }
}

OutputUpdate TrialStateMachine::leave(ExitCause cause, uint8_t trans_index,
                                      Microseconds now_us) {
  record(cause, trans_index, now_us);
  const State& s = graph_->states[current_];
  OutputUpdate ops = apply_actions(s.exit_first, s.exit_count);
  // Everything this state raised comes down, whether or not the graph said so.
  // A valve left open because a graph forgot an on_exit is not an acceptable
  // failure mode, so the engine guarantees it rather than trusting the data.
  ops.set_low |= raised_ & ~ops.set_high;
  raised_ = 0;
  return ops;
}

bool TrialStateMachine::cancel(CancelReason why, Microseconds now_us) {
  if (!running_) return false;  // first terminal decision wins
  OutputUpdate ops = leave(ExitCause::Cancel, kNoTransition, now_us);
  (void)ops;  // the caller re-reads via the next scan; see note below
  result_.outcome = Outcome::Cancelled;
  result_.cancel_reason = why;
  result_.total_us = since(started_us_, now_us);
  running_ = false;
  pending_ = ops;
  has_pending_ = true;
  return true;
}

OutputUpdate TrialStateMachine::scan(LineBitmask word, Microseconds now_us) {
  OutputUpdate ops;
  if (has_pending_) {  // outputs owed by a cancel that happened between scans
    ops = pending_;
    has_pending_ = false;
    pending_ = OutputUpdate{};
  }
  if (!running_ || graph_ == nullptr) return ops;

  // The runtime cap. Validation proves a terminal state is reachable; it cannot
  // prove one is reached.
  if (trial_cap_ms_ > 0 &&
      since(started_us_, now_us) >= static_cast<uint32_t>(trial_cap_ms_) * 1000u) {
    cancel(CancelReason::TrialTimeout, now_us);
    if (has_pending_) {
      ops.set_high |= pending_.set_high;
      ops.set_low |= pending_.set_low;
      has_pending_ = false;
      pending_ = OutputUpdate{};
    }
    return ops;
  }

  const State& s = graph_->states[current_];
  uint8_t next = kNoState;
  uint8_t fired = kNoTransition;
  ExitCause cause = ExitCause::Timeout;

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
  for (uint8_t c = 0; need_eval && c < s.trans_count && next == kNoState; ++c) {
    const uint8_t i = s.trans_first + c;
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
    cause = ExitCause::Transition;
  }

  if (next == kNoState && timeout_ms_ >= 0 &&
      since(entered_us_, now_us) >= static_cast<uint32_t>(timeout_ms_) * 1000u) {
    next = s.timeout_target;
    cause = ExitCause::Timeout;
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
    result_.outcome = target.outcome;
    result_.total_us = since(started_us_, now_us);
    current_ = next;
    record(ExitCause::Terminal, kNoTransition, now_us);
    running_ = false;
    return ops;
  }

  enter(next, now_us, word);
  const OutputUpdate entry_ops = apply_actions(target.entry_first, target.entry_count);
  ops.set_high |= entry_ops.set_high;
  ops.set_low = (ops.set_low | entry_ops.set_low) & ~entry_ops.set_high;
  raised_ |= entry_ops.set_high;
  return ops;
}

}  // namespace fsmd
