// SPDX-License-Identifier: AGPL-3.0-or-later
//! Which outcomes a terminal state may declare.
//!
//! The taxonomy itself is `wire::braemons::v1::TrialOutcome`, vendored from
//! `contracts/` and shared with triald. What is here is the *subset a graph may
//! name*, which is a rule about authoring rather than about the taxonomy.

use crate::wire::braemons::v1::TrialOutcome;

/// The two codes a terminal state may **not** declare.
///
/// `UNDETERMINED` is a state a trial passes through rather than one it can end
/// in, and a graph naming it would produce a trial that reported "still
/// running" for ever. `NEVER_FINISHED` is triald's verdict about its own
/// silence: a terminal state declaring "nobody heard from me" is a
/// contradiction, and only the host can assign it.
const NOT_DECLARABLE: [TrialOutcome; 2] = [TrialOutcome::Undetermined, TrialOutcome::NeverFinished];

/// Every outcome a terminal state may declare, by the name a graph file uses.
///
/// Derived from the generated enum rather than written out, so an outcome added
/// to `braemons/v1/trial_outcome.proto` is declarable here without this file
/// changing — the same property the Python `DECLARABLE_TERMINAL_OUTCOMES`
/// comprehension has.
pub fn declarable() -> Vec<&'static str> {
    let mut names: Vec<&'static str> = [
        TrialOutcome::NotStarted,
        TrialOutcome::Undetermined,
        TrialOutcome::Hit,
        TrialOutcome::WrongResponse,
        TrialOutcome::EarlyHit,
        TrialOutcome::EarlyWrongResponse,
        TrialOutcome::Early,
        TrialOutcome::Late,
        TrialOutcome::EyeError,
        TrialOutcome::UnexpectedStartSignal,
        TrialOutcome::WrongStartSignal,
        TrialOutcome::Cancelled,
        TrialOutcome::NeverFinished,
    ]
    .into_iter()
    .filter(|outcome| !NOT_DECLARABLE.contains(outcome))
    .map(|outcome| outcome.as_str_name())
    .collect();
    names.sort_unstable();
    names
}

/// The outcome a graph's `outcome:` field names, or `None`.
pub fn terminal_outcome_for_name(name: &str) -> Option<TrialOutcome> {
    TrialOutcome::from_str_name(name).filter(|outcome| !NOT_DECLARABLE.contains(outcome))
}
