// SPDX-License-Identifier: AGPL-3.0-or-later
//! One trial, from arming to result — and the link thread that hears it.
//!
//! **The loop.** triald drives it and a person on a bench drives the same four
//! rpcs; there is no private path for either (`contracts/INTERACTIONS.md` §2).
//!
//! What arrives unasked — every state a trial enters, the chunks of its result
//! — is read by one thread between requests and by every request while it
//! waits for its reply. Either way it lands in the device's event queue, and
//! `with_device` is what drains that queue into the trace, under the same lock,
//! before the daemon writes its own entry about the command that was sent.

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use super::refusal::{Category, Refusal};
use super::{poisoned, status_for_device, trace_fields};
use crate::daemon_state::DaemonState;
use crate::device::state_visit_trace::{
    KIND_LINK_LOST, KIND_SEQUENCE_GAP, KIND_STATE_VISIT, KIND_TRIAL_CANCELLED,
    KIND_TRIAL_CONFIGURED, KIND_TRIAL_RESULT, KIND_TRIAL_STARTED,
};
use crate::device::statemachined_device::{
    DeviceEvent, ObservedStateVisit, StatemachinedDevice, TrialConfiguration, TrialProblem,
};
use crate::model::trial_record::TrialResultRecord;
use crate::wire::statemachined::v1 as wire;

/// How long one pass of the link thread holds the board. Short, so a request
/// never waits long behind an idle link.
const PUMP_BUDGET: Duration = Duration::from_millis(50);

impl From<TrialProblem> for Refusal {
    fn from(problem: TrialProblem) -> Self {
        match problem {
            TrialProblem::Device(problem) => problem.into(),
            TrialProblem::NoGraphSetCommitted(sentence) => {
                Refusal::new(Category::WrongMoment, "no_graph_set", sentence, "graph")
            }
            TrialProblem::NotInSet(problem) => problem.into(),
        }
    }
}

fn refused(problem: impl Into<Refusal>) -> tonic::Status {
    problem.into().into()
}

impl DaemonState {
    /// Work on the board, and write down what it said meanwhile.
    ///
    /// Every command the daemon sends goes through here, so a visit that
    /// arrived while a `configure` was in flight is in the trace before the
    /// `trial_configured` entry — which is the order it happened in.
    pub(super) fn with_device<T>(
        &self,
        work: impl FnOnce(&mut StatemachinedDevice) -> T,
    ) -> Result<T, tonic::Status> {
        let mut device = self.device.lock().map_err(poisoned)?;
        let answer = work(&mut device);
        for event in device.take_events() {
            self.record(&device, event);
        }
        Ok(answer)
    }

    fn record(&self, device: &StatemachinedDevice, event: DeviceEvent) {
        match event {
            DeviceEvent::Visit(observed) => self.record_state_visit(device, observed),
            DeviceEvent::Result(result) => self.record_trial_result(result),
            // Handed to nobody, as Python's `RigService` hands them to nobody:
            // the serial monitor already holds every line.
            DeviceEvent::Unsolicited(_) => {}
        }
    }

    /// One `visit` into the ring, named and placed in host time.
    ///
    /// A gap in the device's per-run `seq` is recorded as its own entry rather
    /// than silently closed over: the stream is a preview and the result is
    /// the record, so a gap here is a thing to reconcile — but only if
    /// somebody wrote down that it happened.
    fn record_state_visit(&self, device: &StatemachinedDevice, observed: ObservedStateVisit) {
        if let Ok(mut last) = self.last_visit_sequence_number.lock() {
            if let Some(expected) = *last {
                if observed.sequence_number > expected + 1 {
                    self.trace.append(
                        KIND_SEQUENCE_GAP,
                        trace_fields(json!({
                            "trial_id": observed.trial_id,
                            "missing_from_sequence_number": expected + 1,
                            "missing_to_sequence_number": observed.sequence_number - 1,
                        })),
                    );
                }
            }
            *last = Some(observed.sequence_number);
        }
        let visit = &observed.visit;
        let host_time = observed.host_time.as_ref();
        self.trace.append(
            KIND_STATE_VISIT,
            trace_fields(json!({
                "trial_id": observed.trial_id,
                "device_sequence_number": observed.sequence_number,
                "graph": device.armed_graph_name,
                "set_version": device.committed_graph_set.as_ref().map(|set| set.set_version),
                "state_name": visit.state_name,
                "exit_cause": visit.exit_cause,
                "fired_transition_target_state_name": visit.fired_transition_target_state_name,
                "drawn_duration_ms": visit.drawn_duration_ms,
                "measured_duration_microseconds": visit.measured_duration_microseconds,
                "entered_device_microseconds": visit.entered_device_microseconds,
                "unwrapped_device_microseconds": observed.unwrapped_device_microseconds as i64,
                "entered_host_time": host_time.map(|time| time.host_time_iso8601()),
                "host_time_uncertainty_microseconds":
                    host_time.map(|time| time.uncertainty_microseconds),
            })),
        );
    }

    fn record_trial_result(&self, result: TrialResultRecord) {
        if let Ok(mut last) = self.last_visit_sequence_number.lock() {
            *last = None;
        }
        self.trace.append(
            KIND_TRIAL_RESULT,
            trace_fields(json!({
                "trial_id": result.trial_id,
                "outcome": result.outcome_name(),
                "cancel_reason": result.cancel_reason_name(),
                "total_duration_microseconds": result.total_duration_microseconds,
                "visit_count": result.visits.len(),
                "total_visit_count": result.total_visit_count,
                "path_was_truncated": result.path_was_truncated,
            })),
        );
        // And that is the end of it. The result is in the trace, which is what
        // anybody watching reads and what stays on the rig if nobody is. This
        // daemon sends it nowhere and waits for nobody.
        if let Ok(mut last) = self.last_trial_result.lock() {
            *last = Some(result);
        }
    }

    // -- the link thread ---------------------------------------------------------

    /// Start the only thread that reads the device when nobody asked it to.
    ///
    /// Short bursts under the lock, so a request never waits long for it. A
    /// heartbeat `ping` goes out on the configured cadence: it arms the
    /// device's link-loss watchdog, and for free it keeps the clock
    /// correlation fresh.
    pub fn read_the_link_forever(self: &Arc<Self>) -> std::thread::JoinHandle<()> {
        let state = self.clone();
        std::thread::Builder::new()
            .name("statemachined-link".into())
            .spawn(move || {
                let heartbeat = Duration::from_secs_f64(state.configuration.heartbeat_seconds);
                let mut last_heartbeat: Option<Instant> = None;
                while !state.stopping.load(Ordering::Relaxed) {
                    if !state.device_connected() {
                        std::thread::sleep(Duration::from_millis(200));
                        continue;
                    }
                    let pass = state.with_device(|device| {
                        device.pump_incoming_lines(PUMP_BUDGET)?;
                        if last_heartbeat.is_none_or(|at| at.elapsed() >= heartbeat) {
                            last_heartbeat = Some(Instant::now());
                            device.send_heartbeat_ping()?;
                        }
                        Ok::<(), crate::device::statemachined_device::DeviceProblem>(())
                    });
                    if let Ok(Err(problem)) = pass {
                        state.note_the_link_went_away(problem.to_string());
                    }
                    std::thread::sleep(Duration::from_millis(5));
                }
            })
            .expect("the link thread starts")
    }

    fn note_the_link_went_away(&self, detail: String) {
        if let Ok(mut last) = self.last_error_from_the_device.lock() {
            *last = Some(detail.clone());
        }
        self.trace
            .append(KIND_LINK_LOST, trace_fields(json!({ "detail": detail })));
        if let Ok(mut device) = self.device.lock() {
            device.disconnect();
        }
    }

    // -- a trial -------------------------------------------------------------------

    /// The graph a trial names, or the active one, or a refusal saying so.
    pub(super) fn graph_for_a_trial(&self, graph_name: &str) -> Result<String, tonic::Status> {
        if !graph_name.is_empty() {
            return Ok(graph_name.to_string());
        }
        if let Some(active) = self.active_graph.lock().map_err(poisoned)?.clone() {
            return Ok(active);
        }
        Err(Refusal::new(
            Category::WrongMoment,
            "no_graph_named",
            "this trial named no graph and no graph is selected as the active one. Name one, \
             or select an active graph first.",
            "graph",
        )
        .into())
    }

    /// Named distributions into pool indices and the wire's `a`/`b`/`c`.
    ///
    /// The same translation the compiler does for an upload, and it happens
    /// here too because a patch names a distribution of a graph that is
    /// **already on the device**, where it is an index into a shared pool and
    /// nothing remembers what it was called. Only `a`, `b` and `c`, never
    /// `kind`: changing a distribution's shape would be a different graph.
    fn distribution_patches_as_wire_fields(
        &self,
        patches: &[wire::DistributionPatch],
        graph_name: &str,
    ) -> Result<Vec<Value>, tonic::Status> {
        if patches.is_empty() {
            return Ok(Vec::new());
        }
        let device = self.device.lock().map_err(poisoned)?;
        let compiled = device.committed_graph_set.as_ref().ok_or_else(|| {
            refused(TrialProblem::NoGraphSetCommitted(
                "no graph set is committed, so no distribution has an index".into(),
            ))
        })?;
        let graph = compiled.graph_named(graph_name).map_err(refused)?;
        patches
            .iter()
            .map(|patch| {
                let Some(index) = graph.distribution_pool_index_by_name.get(&patch.name) else {
                    let mut known: Vec<&str> = graph
                        .distribution_pool_index_by_name
                        .keys()
                        .map(String::as_str)
                        .collect();
                    known.sort_unstable();
                    return Err(refused(crate::graph_set_compiler::CompileError::Set(
                        format!(
                            "graph '{graph_name}' has no distribution called '{}'. Has: {}",
                            patch.name,
                            known.join(", ")
                        ),
                    )));
                };
                let mut fields = serde_json::Map::new();
                fields.insert("i".into(), json!(index));
                // Positional on the wire by `kind`; named here. Later wins, as
                // in Python: `minimum_ms` over `duration_ms` for `a`.
                if let Some(duration) = patch.duration_ms {
                    fields.insert("a".into(), json!(duration));
                }
                if let Some(minimum) = patch.minimum_ms {
                    fields.insert("a".into(), json!(minimum));
                }
                if let Some(maximum) = patch.maximum_ms {
                    fields.insert("b".into(), json!(maximum));
                }
                if let Some(mean) = patch.mean_ms {
                    fields.insert("c".into(), json!(mean));
                }
                Ok(Value::Object(fields))
            })
            .collect()
    }

    /// Arm the device for one trial. Nothing is uploaded.
    pub(super) fn configure_trial(
        &self,
        request: &wire::ConfigureTrialRequest,
    ) -> Result<wire::ConfigureTrialResult, tonic::Status> {
        let seam = |detail: &str, context: &str| -> tonic::Status {
            Refusal::new(Category::BadRequest, "bad_request", detail, context).into()
        };
        // What arrived, checked at the seam: a command from another machine,
        // and what it says is what the board will be armed with.
        if request.trial_id < 0 {
            return Err(seam("a trial id is never negative", "trial_id"));
        }
        if request.cap_milliseconds < 0 {
            return Err(seam("a cap is never negative", "cap_milliseconds"));
        }
        for patch in &request.distribution_patches {
            if patch.name.is_empty() {
                return Err(seam("a distribution patch names no distribution", "name"));
            }
            let sets_something = patch.minimum_ms.is_some()
                || patch.maximum_ms.is_some()
                || patch.mean_ms.is_some()
                || patch.duration_ms.is_some();
            if !sets_something {
                return Err(seam(
                    &format!("the patch for '{}' sets no parameter", patch.name),
                    "distribution_patches",
                ));
            }
        }
        // An empty name means the active graph, and which that is is the
        // session's business.
        let graph_name = self.graph_for_a_trial(&request.graph)?;
        let trial = TrialConfiguration {
            trial_id: request.trial_id,
            graph_name: graph_name.clone(),
            cap_milliseconds: request.cap_milliseconds as i64,
            start_source: if request.start_source.is_empty() {
                "serial".into()
            } else {
                request.start_source.clone()
            },
            start_line: request.start_line.map(i64::from),
            timers: None,
            distribution_patches: self
                .distribution_patches_as_wire_fields(&request.distribution_patches, &graph_name)?,
        };
        let armed = self
            .with_device(|device| device.configure_trial(&trial))?
            .map_err(refused)?;
        let number = |key: &str| armed.get(key).and_then(Value::as_i64);
        self.trace.append(
            KIND_TRIAL_CONFIGURED,
            trace_fields(json!({
                "trial_id": request.trial_id,
                "graph": graph_name,
                "set_version": armed.get("set_version"),
                "graph_index": armed.get("graph_index"),
            })),
        );
        Ok(wire::ConfigureTrialResult {
            trial_id: request.trial_id,
            graph: graph_name,
            set_version: number("set_version").unwrap_or(0) as i32,
            graph_index: number("graph_index").unwrap_or(0) as i32,
            elapsed_milliseconds: number("elapsed_milliseconds").unwrap_or(0) as i32,
        })
    }

    pub(super) fn start_trial(&self, trial_id: i64) -> Result<Value, tonic::Status> {
        let started = self
            .with_device(|device| device.start_trial(trial_id))?
            .map_err(status_for_device)?;
        self.trace.append(
            KIND_TRIAL_STARTED,
            trace_fields(json!({
                "trial_id": trial_id,
                "started_device_microseconds": started.get("at_us"),
            })),
        );
        Ok(started)
    }

    pub(super) fn cancel_trial(&self, trial_id: i64) -> Result<Value, tonic::Status> {
        self.try_to_cancel(trial_id)?.map_err(status_for_device)
    }

    /// Cancel, keeping the device's own refusal as itself: `Close` records it
    /// in `last_error` in the device's words rather than as a status.
    pub(super) fn try_to_cancel(
        &self,
        trial_id: i64,
    ) -> Result<Result<Value, crate::device::statemachined_device::DeviceProblem>, tonic::Status>
    {
        let acknowledgement = match self.with_device(|device| device.cancel_trial(trial_id))? {
            Ok(acknowledgement) => acknowledgement,
            Err(problem) => return Ok(Err(problem)),
        };
        self.trace.append(
            KIND_TRIAL_CANCELLED,
            trace_fields(json!({
                "trial_id": trial_id,
                "cancelled": acknowledgement.get("cancelled"),
                "outcome_code": acknowledgement.get("outcome"),
            })),
        );
        Ok(Ok(acknowledgement))
    }

    // -- what the rig is doing ------------------------------------------------------

    /// One reading, with the state index resolved to a name where it can be.
    ///
    /// **"Where it can be" is the whole of it, and it must never refuse.** The
    /// armed graph is the last one this daemon armed, and the committed set
    /// can have been replaced since. The name is a convenience over
    /// `state_index`, which is always there, and a `ReadState` that refused
    /// over a label would take the whole panel down.
    pub(super) fn rig_state(&self) -> Result<wire::RigState, tonic::Status> {
        let report = self.read_device_state()?;
        let device = self.device.lock().map_err(poisoned)?;
        let index = report.get("current_state").and_then(Value::as_i64);
        let state_name = (|| {
            let graph = device
                .committed_graph_set
                .as_ref()?
                .graph_named(device.armed_graph_name.as_deref()?)
                .ok()?;
            graph
                .state_names_by_index
                .get(usize::try_from(index?).ok()?)
                .cloned()
        })();
        let io = report.get("io");
        let word = |key: &str| io.and_then(|io| io.get(key)).and_then(Value::as_i64);
        let number = |source: Option<&Value>, key: &str| {
            source
                .and_then(|s| s.get(key))
                .and_then(Value::as_i64)
                .unwrap_or(0)
        };
        let scan = report.get("scan");
        Ok(wire::RigState {
            connected: device.is_connected(),
            link_state: number(Some(&report), "link_state") as i32,
            running: report
                .get("running")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            // Every one of these is optional because zero is a real answer
            // for all four: trial 0, state 0, and a word with nothing pressed.
            trial_id: report.get("trial_id").and_then(Value::as_i64),
            graph: device.armed_graph_name.clone().unwrap_or_default(),
            state_name,
            state_index: index.map(|index| index as i32),
            input_word: word("in"),
            output_word: word("out"),
            scan: Some(wire::ScanHealth {
                hz: number(scan, "hz") as i32,
                overruns: number(scan, "overruns"),
                worst_gap: number(scan, "worst_gap") as i32,
                tx_stalls: number(scan, "tx_stalls"),
            }),
            newest_trace_entry_number: self.trace.newest_entry_number(),
        })
    }
}

/// A result, as triald will record it. The outcome crosses as its `.tdr`
/// number, which is never renumbered.
pub(super) fn trial_result_to_wire(record: &TrialResultRecord) -> wire::TrialResult {
    wire::TrialResult {
        trial_id: record.trial_id,
        outcome: record.outcome,
        cancel_reason: record.cancel_reason,
        total_duration_microseconds: record.total_duration_microseconds,
        visits: record
            .visits
            .iter()
            .map(|visit| wire::StateVisit {
                state_name: visit.state_name.clone(),
                exit_cause: visit.exit_cause.clone(),
                // Absent, not zero: transition zero is a real transition.
                fired_transition_position: visit.fired_transition_position.map(|p| p as i32),
                fired_transition_target_state_name: visit
                    .fired_transition_target_state_name
                    .clone(),
                drawn_duration_ms: visit.drawn_duration_ms as i32,
                entered_device_microseconds: visit.entered_device_microseconds,
                measured_duration_microseconds: visit.measured_duration_microseconds,
            })
            .collect(),
        path_was_truncated: record.path_was_truncated,
        first_visit_sequence_number: record.first_visit_sequence_number,
        total_visit_count: record.total_visit_count,
    }
}
