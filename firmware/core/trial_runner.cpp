#include "trial_runner.h"

namespace fsmd {

OutputUpdate TrialRunner::start(uint32_t trial_id, uint64_t session_seed, Microseconds now_us,
                                LineBitmask word) {
  result_ = TrialRecord{};
  result_.trial_id = trial_id;
  return machine_.start(Rng::mix(session_seed, trial_id), now_us, word);
}

OutputUpdate TrialRunner::scan(LineBitmask word, Microseconds now_us) {
  OutputUpdate ops = machine_.scan(word, now_us);
  settle();
  return ops;
}

bool TrialRunner::cancel(TrialCancelReason why, Microseconds now_us) {
  if (!machine_.halt(now_us)) return false;
  result_.outcome = TrialOutcome::Cancelled;
  result_.cancel_reason = why;
  return true;
}

void TrialRunner::settle() {
  if (result_.outcome != TrialOutcome::Undetermined) return;  // first verdict wins
  const RunRecord& r = machine_.record();
  if (r.terminal_code != kNotTerminal) {
    result_.outcome = outcome_of(r.terminal_code);
  } else if (r.hit_run_cap) {
    // The machine stopped itself on the wall-clock cap. Only the trial layer
    // knows that is a cancellation rather than simply the end of a run.
    result_.outcome = TrialOutcome::Cancelled;
    result_.cancel_reason = TrialCancelReason::TrialTimeout;
  }
}

}  // namespace fsmd
