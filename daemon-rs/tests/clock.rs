// SPDX-License-Identifier: AGPL-3.0-or-later
//! The clock correlation, replayed against what Python answered.
//!
//! **Every timestamp in every recording depends on this arithmetic**, so it is
//! the one piece of the device layer where "close enough" is not a thing. The
//! plan said to check it against recordings the Python daemon made; there are
//! no recent ones, and a sweep is the better instrument anyway. A recording
//! covers whatever happened on some Tuesday — the straight line through the
//! middle. `clock_trace.json`, written by a sweep over the Python daemon's
//! arithmetic before it was retired, covers what the arithmetic actually turns on:
//! the wrap boundary from both sides, a backwards jump just under the
//! threshold, a jump of exactly the threshold, several wraps in a row, a ping
//! worse than the one before it, a round trip that rounds rather than
//! truncates, and a reconnect.

use serde::Deserialize;
use statemachined::device::device_clock_correlation::DeviceClockCorrelation;

#[derive(Debug, Deserialize)]
struct Step {
    op: String,
    why: String,
    #[serde(default)]
    raw: Option<i128>,
    #[serde(default)]
    unwrapped: Option<i128>,
    #[serde(default)]
    before: Option<f64>,
    #[serde(default)]
    after: Option<f64>,
    answer: Answer,
}

#[derive(Debug, Deserialize)]
struct Answer {
    #[serde(default)]
    unwrapped: Option<i128>,
    #[serde(default)]
    refused: Option<bool>,
    /// Absent on steps that are not a ping; `null` when there is no best yet.
    #[serde(default)]
    best_round_trip_microseconds: Option<i64>,
    /// `null` when Python gave no estimate, which is a real answer and not an
    /// absence: the query happened and there was nothing to say.
    #[serde(default)]
    estimate: Option<Estimate>,
    has_an_estimate: bool,
}

#[derive(Debug, Deserialize)]
struct Estimate {
    /// Python's `repr`, so a JSON round trip is not what decides agreement.
    host_unix_seconds: String,
    uncertainty_microseconds: i64,
    host_time_iso8601: String,
}

fn trace() -> Vec<Step> {
    serde_json::from_str(include_str!("clock_trace.json"))
        .expect("clock_trace.json, recorded from the Python daemon")
}

#[test]
fn the_trace_covers_what_it_claims_to() {
    // A guard on the guard. A trace that lost its wrap cases would pass every
    // assertion below and prove nothing about the part that is hard.
    let trace = trace();
    assert!(trace.len() > 30, "only {} operations", trace.len());
    for wanted in ["wrap", "threshold", "ping", "forget"] {
        assert!(
            trace.iter().any(|step| step.why.contains(wanted) || step.op == wanted),
            "the trace has no {wanted} case"
        );
    }
}

#[test]
fn every_operation_answers_the_way_python_did() {
    let mut clock = DeviceClockCorrelation::new();
    let mut checked = 0;

    for (at, step) in trace().iter().enumerate() {
        let where_ = format!("step {at} ({}): {}", step.op, step.why);
        match step.op.as_str() {
            "unwrap" => {
                let ours = clock.unwrap_device_microseconds(step.raw.expect("a raw reading"));
                assert_eq!(
                    ours,
                    step.answer.unwrapped.expect("python unwrapped it"),
                    "{where_}"
                );
                checked += 1;
            }
            "ping" => {
                let refused = clock
                    .observe_ping_round_trip(
                        step.before.expect("a before"),
                        step.raw.expect("a raw reading"),
                        step.after.expect("an after"),
                    )
                    .is_err();
                assert_eq!(refused, step.answer.refused.expect("python said"), "{where_}");
                assert_eq!(
                    clock.best_round_trip_microseconds(),
                    step.answer.best_round_trip_microseconds,
                    "{where_}: the best round trip is what the bound rests on"
                );
                checked += 1;
            }
            "query" => {
                let ours =
                    clock.host_time_for_unwrapped_device_microseconds(step.unwrapped.expect("a value"));
                match (ours, step.answer.estimate.as_ref()) {
                    (None, None) => {}
                    (Some(ours), Some(theirs)) => {
                        assert_eq!(
                            ours.uncertainty_microseconds, theirs.uncertainty_microseconds,
                            "{where_}: the bound"
                        );
                        assert_eq!(
                            ours.host_time_iso8601(),
                            theirs.host_time_iso8601,
                            "{where_}: the rendering"
                        );
                        // The seconds themselves, to the microsecond. Compared
                        // as a number rather than as text, because Python's
                        // repr and Rust's Display disagree about how to print
                        // a float that both hold identically.
                        let expected: f64 =
                            theirs.host_unix_seconds.parse().expect("python's repr parses");
                        assert!(
                            (ours.host_unix_seconds - expected).abs() < 1e-6,
                            "{where_}: {} against {expected}",
                            ours.host_unix_seconds
                        );
                    }
                    (ours, theirs) => panic!("{where_}: {ours:?} against {theirs:?}"),
                }
                checked += 1;
            }
            "forget" => clock.forget_everything_observed(),
            other => panic!("the trace has an operation this test does not know: {other}"),
        }
        assert_eq!(
            clock.has_an_estimate(),
            step.answer.has_an_estimate,
            "{where_}: whether there is an estimate at all"
        );
    }
    assert!(checked > 25, "only {checked} operations actually asserted");
}

#[test]
fn a_reply_that_arrives_before_its_request_is_refused() {
    // Not in the trace because Python raises rather than answering, and a trace
    // of exceptions is a worse thing to compare than one of values.
    let mut clock = DeviceClockCorrelation::new();
    assert!(clock
        .observe_ping_round_trip(1_700_000_000.010, 0, 1_700_000_000.000)
        .is_err());
    assert!(!clock.has_an_estimate(), "a refused ping left an estimate");
}

#[test]
fn a_fraction_that_rounds_up_to_a_whole_second_still_prints_as_a_time() {
    // `round((x - int(x)) * 1e6)` reaches 1_000_000 for a fraction within half
    // a microsecond of a whole second, which formatted as six digits is seven:
    // `...:20.1000000Z`, on a second that is also wrong by one.
    //
    // **Found by writing this port.** The Python implementation had the same
    // hole and was fixed in its own commit; this is the Rust half of the pair,
    // and both are now tested for it.
    use statemachined::device::device_clock_correlation::HostTimeEstimate;

    let estimate = HostTimeEstimate {
        host_unix_seconds: 1_700_000_000.999_999_9,
        uncertainty_microseconds: 0,
    };
    let rendered = estimate.host_time_iso8601();
    assert_eq!(rendered, "2023-11-14T22:13:21.000000Z", "{rendered}");
    assert_eq!(rendered.len(), "2023-11-14T22:13:20.000000Z".len());
}

#[test]
fn the_rendering_is_utc_and_to_the_microsecond() {
    use statemachined::device::device_clock_correlation::HostTimeEstimate;
    let at = |seconds: f64| {
        HostTimeEstimate {
            host_unix_seconds: seconds,
            uncertainty_microseconds: 0,
        }
        .host_time_iso8601()
    };
    assert_eq!(at(0.0), "1970-01-01T00:00:00.000000Z");
    assert_eq!(at(1_700_000_000.0), "2023-11-14T22:13:20.000000Z");
    // A leap day, which is where a hand-written civil calendar goes wrong.
    assert_eq!(at(1_709_164_800.0), "2024-02-29T00:00:00.000000Z");
}
