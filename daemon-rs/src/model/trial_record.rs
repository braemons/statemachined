// SPDX-License-Identifier: AGPL-3.0-or-later
//! What came back from a trial, with the indices turned back into names.
//!
//! The device reports a path of six-element arrays (protocol.md 4.3) and the
//! `visit` stream reports the same rows as they happen (4.4). Both are
//! indices, because the device has 32 KB. Both are decoded here, by the same
//! code, into the names the graph was authored with — so an analysis reads
//! `Foreperiod` and not `state 2`.
//!
//! Nothing here interprets. A record is evidence, and a log is exactly where an
//! opinion gets smuggled in unnoticed.

use serde_json::Value;

use crate::wire::braemons::v1::TrialOutcome;
use crate::wire::statemachined::v1::TrialCancelReason;

/// `transition_index` when the state was not left by a transition: the wire's
/// `kNoTransition`.
pub const NO_TRANSITION_FIRED: i64 = 255;

/// The elements in a `result_path` row and in a `visit`'s `v`. One shape for
/// one fact: two would be how the record and the stream drift apart.
pub const WIRE_ROW_LENGTH: usize = 6;

/// One state, entered and left, with everything the device measured about it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StateVisitRecord {
    pub state_name: String,
    /// `timeout`, `transition`, `cancel` or `terminal`.
    pub exit_cause: String,
    /// Which of this state's transitions fired, counted from zero in the order
    /// the graph declares them, or `None` for an exit that was not one.
    pub fired_transition_position: Option<i64>,
    /// Where that transition led. `None` when nothing fired, and also when the
    /// graph no longer has that edge.
    pub fired_transition_target_state_name: Option<String>,
    /// What the device *drew* for this state's timeout, in milliseconds.
    pub drawn_duration_ms: i64,
    /// Device clock, microseconds. Wraps every ~71 minutes.
    pub entered_device_microseconds: i64,
    /// What actually happened; `drawn_duration_ms` is what was asked for.
    pub measured_duration_microseconds: i64,
}

/// A whole trial's result, reassembled and named.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TrialResultRecord {
    pub trial_id: i64,
    /// The `.tdr` number. Kept as the number because a board newer than this
    /// daemon may report one this build has never heard of.
    pub outcome: i32,
    pub cancel_reason: i32,
    pub total_duration_microseconds: i64,
    pub visits: Vec<StateVisitRecord>,
    /// The device's path buffer is a ring: a long looping trial arrives as a
    /// window, holding the last `visits.len()` of `total_visit_count`.
    pub path_was_truncated: bool,
    pub first_visit_sequence_number: i64,
    pub total_visit_count: i64,
}

impl TrialResultRecord {
    /// How many visits the ring dropped. Zero unless it wrapped.
    pub fn missing_visit_count(&self) -> i64 {
        (self.total_visit_count - self.visits.len() as i64).max(0)
    }

    /// The outcome's name, or the number where this build does not know it.
    pub fn outcome_name(&self) -> String {
        TrialOutcome::try_from(self.outcome)
            .map(|outcome| outcome.as_str_name().to_string())
            .unwrap_or_else(|_| self.outcome.to_string())
    }

    /// The cancel reason as Python's `TrialCancelReason.name` spells it.
    pub fn cancel_reason_name(&self) -> String {
        TrialCancelReason::try_from(self.cancel_reason)
            .map(|reason| {
                reason
                    .as_str_name()
                    .trim_start_matches("TRIAL_CANCEL_REASON_")
                    .to_string()
            })
            .unwrap_or_else(|_| self.cancel_reason.to_string())
    }
}

/// One wire row into a named visit.
///
/// Decoding is only possible against the graph the trial actually ran, which
/// is what keeps a renamed state from silently mislabelling last week's data.
/// An index the graph does not explain is reported rather than guessed at.
pub fn decode_state_visit_row(
    row: &[Value],
    state_names_by_index: &[String],
    transition_target_names_by_state_index: &[Vec<String>],
) -> Result<StateVisitRecord, String> {
    if row.len() != WIRE_ROW_LENGTH {
        return Err(format!(
            "a path row has {} elements, not {WIRE_ROW_LENGTH}: {}",
            row.len(),
            Value::from(row.to_vec())
        ));
    }
    let state_index = row[0]
        .as_i64()
        .filter(|index| (0..state_names_by_index.len() as i64).contains(index))
        .ok_or_else(|| {
            format!(
                "a path row names state {}, and the graph has {} states",
                row[0],
                state_names_by_index.len()
            )
        })? as usize;
    let number = |value: &Value| {
        value
            .as_i64()
            .or_else(|| value.as_f64().map(|f| f as i64))
            .unwrap_or(0)
    };
    let transition_index = number(&row[2]);
    let (fired_position, fired_target) = if transition_index == NO_TRANSITION_FIRED {
        (None, None)
    } else {
        let target = transition_target_names_by_state_index
            .get(state_index)
            .and_then(|targets| {
                usize::try_from(transition_index)
                    .ok()
                    .and_then(|at| targets.get(at))
            })
            .cloned();
        (Some(transition_index), target)
    };
    Ok(StateVisitRecord {
        state_name: state_names_by_index[state_index].clone(),
        exit_cause: match &row[1] {
            Value::String(cause) => cause.clone(),
            other => other.to_string(),
        },
        fired_transition_position: fired_position,
        fired_transition_target_state_name: fired_target,
        drawn_duration_ms: number(&row[3]),
        entered_device_microseconds: number(&row[4]),
        measured_duration_microseconds: number(&row[5]),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn names() -> (Vec<String>, Vec<Vec<String>>) {
        let states = ["Wait", "Cue", "Hit", "Aborted"].map(String::from).to_vec();
        let targets = vec![
            vec!["Aborted".to_string()],
            vec!["Hit".to_string(), "Aborted".to_string()],
            vec![],
            vec![],
        ];
        (states, targets)
    }

    fn row(value: Value) -> Vec<Value> {
        value.as_array().unwrap().clone()
    }

    #[test]
    fn a_row_becomes_the_names_the_graph_was_written_with() {
        let (states, targets) = names();
        let visit = decode_state_visit_row(
            &row(json!([1, "transition", 0, 500, 500120, 183044])),
            &states,
            &targets,
        )
        .unwrap();
        assert_eq!(visit.state_name, "Cue");
        assert_eq!(visit.exit_cause, "transition");
        assert_eq!(visit.fired_transition_position, Some(0));
        assert_eq!(
            visit.fired_transition_target_state_name.as_deref(),
            Some("Hit")
        );
        assert_eq!(visit.drawn_duration_ms, 500);
        assert_eq!(visit.measured_duration_microseconds, 183044);
    }

    #[test]
    fn the_transition_index_is_read_against_the_state_that_fired_it() {
        let (states, targets) = names();
        let visit = decode_state_visit_row(
            &row(json!([1, "transition", 1, 0, 0, 10])),
            &states,
            &targets,
        )
        .unwrap();
        assert_eq!(
            visit.fired_transition_target_state_name.as_deref(),
            Some("Aborted")
        );
    }

    #[test]
    fn an_exit_that_was_not_a_transition_names_none() {
        let (states, targets) = names();
        let visit = decode_state_visit_row(
            &row(json!([0, "timeout", 255, 500, 0, 500120])),
            &states,
            &targets,
        )
        .unwrap();
        assert_eq!(visit.fired_transition_position, None);
        assert_eq!(visit.fired_transition_target_state_name, None);
    }

    #[test]
    fn a_state_index_the_graph_cannot_explain_is_a_finding_not_a_guess() {
        let (states, targets) = names();
        let refused =
            decode_state_visit_row(&row(json!([9, "timeout", 255, 0, 0, 0])), &states, &targets)
                .unwrap_err();
        assert!(
            refused.contains("names state 9, and the graph has 4 states"),
            "{refused}"
        );
    }

    #[test]
    fn a_row_of_the_wrong_length_is_refused() {
        let (states, targets) = names();
        let refused =
            decode_state_visit_row(&row(json!([0, "timeout", 255, 0, 0])), &states, &targets)
                .unwrap_err();
        assert!(refused.contains("has 5 elements, not 6"), "{refused}");
    }

    #[test]
    fn a_truncated_path_says_how_much_is_missing() {
        let result = TrialResultRecord {
            trial_id: 193,
            outcome: TrialOutcome::Hit as i32,
            cancel_reason: 0,
            total_duration_microseconds: 0,
            visits: vec![],
            path_was_truncated: true,
            first_visit_sequence_number: 45,
            total_visit_count: 300,
        };
        assert_eq!(result.missing_visit_count(), 300);
    }

    #[test]
    fn names_are_the_ones_python_writes() {
        let result = TrialResultRecord {
            trial_id: 1,
            outcome: TrialOutcome::Hit as i32,
            cancel_reason: TrialCancelReason::LinkLost as i32,
            total_duration_microseconds: 0,
            visits: vec![],
            path_was_truncated: false,
            first_visit_sequence_number: 0,
            total_visit_count: 0,
        };
        assert_eq!(result.outcome_name(), "HIT");
        assert_eq!(result.cancel_reason_name(), "LINK_LOST");
    }
}
