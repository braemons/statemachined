// SPDX-License-Identifier: AGPL-3.0-or-later
//! Recordings, and the gaps in them.
//!
//! The one shape worth reading twice is `segments`. **A recording with a pause
//! in it has no contiguous range of entry numbers**, because the numbers are
//! the trace's and the gap is real. The segments are where that gap is written
//! down; without them a reader sees a jump and has to guess whether it was a
//! pause or a loss.
//!
//! The order of each lifecycle entry against its verb is the only subtle thing
//! here: the entry lands **inside** the recording it is about. A pause is traced
//! before the segment closes and a resume after the next one opens, so a
//! recording explains its own gaps rather than leaving a reader to infer them.

use serde_json::{json, Value};

use super::refusal::{Category, Refusal};
use super::{poisoned, trace, trace_fields, DaemonServices};
use crate::device::state_visit_trace::{
    KIND_RECORDING_CLEARED, KIND_RECORDING_PAUSED, KIND_RECORDING_RESUMED, KIND_RECORDING_STARTED,
    KIND_RECORDING_STOPPED,
};
use crate::event_recording::{Manifest, RecordingProblem};
use crate::wire::statemachined::v1 as wire;
use crate::wire::statemachined::v1::service;

impl From<RecordingProblem> for Refusal {
    fn from(problem: RecordingProblem) -> Self {
        let (category, error, context) = match &problem {
            RecordingProblem::NotInStore(_) => (Category::NoSuchThing, "no_such_recording", "name"),
            RecordingProblem::BadName(_) => (Category::BadRequest, "bad_recording_name", "name"),
            RecordingProblem::NameTaken(_) => {
                (Category::BadRequest, "recording_name_taken", "name")
            }
            // "Stop it first" and "there is nothing open" are different things
            // to do, and `error` is what a caller branches on.
            RecordingProblem::InProgress(_) => {
                (Category::WrongMoment, "recording_in_progress", "name")
            }
            RecordingProblem::AlreadyOpen(_) => {
                (Category::WrongMoment, "already_recording", "recording")
            }
            RecordingProblem::StateRefused(_) => {
                (Category::WrongMoment, "recording_state", "recording")
            }
        };
        Refusal::new(category, error, problem.to_string(), context)
    }
}

fn refused(problem: RecordingProblem) -> tonic::Status {
    Refusal::from(problem).into()
}

/// One stretch that was actually being written. `from_entry_number` is absent
/// on a segment opened and never written to, and zero is a real entry number.
fn segment_to_wire(segment: &Value) -> wire::RecordingSegment {
    let text = |key: &str| {
        segment
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string()
    };
    wire::RecordingSegment {
        from_entry_number: segment.get("from_entry_number").and_then(Value::as_i64),
        to_entry_number: segment.get("to_entry_number").and_then(Value::as_i64),
        started_host_time: text("started_host_time"),
        ended_host_time: text("ended_host_time"),
        entry_count: segment
            .get("entry_count")
            .and_then(Value::as_i64)
            .unwrap_or(0),
    }
}

fn segments_of(manifest: &Manifest) -> Vec<wire::RecordingSegment> {
    manifest
        .get("segments")
        .and_then(Value::as_array)
        .map(|segments| segments.iter().map(segment_to_wire).collect())
        .unwrap_or_default()
}

/// What a recording is, without its entries. An unreadable manifest arrives
/// as `{name, unreadable}` and crosses as itself.
fn manifest_to_wire(manifest: &Manifest) -> wire::RecordingManifest {
    let text = |key: &str| match manifest.get(key) {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(text)) => text.clone(),
        Some(other) => other.to_string(),
    };
    wire::RecordingManifest {
        name: text("name"),
        description: text("description"),
        state_machine_config: text("state_machine_config"),
        created_unix_seconds: manifest
            .get("created_unix_seconds")
            .and_then(Value::as_f64)
            .unwrap_or(0.0),
        created_host_time: text("created_host_time"),
        state: text("state"),
        segments: segments_of(manifest),
        entry_count: manifest
            .get("entry_count")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        kind_counts: manifest
            .get("kind_counts")
            .and_then(Value::as_object)
            .map(|counts| {
                counts
                    .iter()
                    .map(|(kind, count)| (kind.clone(), count.as_i64().unwrap_or(0)))
                    .collect()
            })
            .unwrap_or_default(),
        unreadable: text("unreadable"),
    }
}

impl DaemonServices {
    fn recordings(&self) -> wire::Recordings {
        let recorder = &self.state.recorder;
        wire::Recordings {
            active: recorder.active().as_ref().map(manifest_to_wire),
            recordings: recorder.manifests().iter().map(manifest_to_wire).collect(),
        }
    }

    fn a_recording_entry(&self, kind: &str, name: &Value, extra: Option<(&str, Value)>) {
        let mut fields = trace_fields(json!({ "recording": name }));
        if let Some((key, value)) = extra {
            fields.insert(key.into(), value);
        }
        self.state.trace.append(kind, fields);
    }
}

#[tonic::async_trait]
impl service::recording_server::Recording for DaemonServices {
    async fn read_recordings(
        &self,
        _request: tonic::Request<wire::ReadRecordingsRequest>,
    ) -> Result<tonic::Response<wire::Recordings>, tonic::Status> {
        Ok(tonic::Response::new(self.recordings()))
    }

    async fn start(
        &self,
        request: tonic::Request<wire::StartRecordingRequest>,
    ) -> Result<tonic::Response<wire::RecordingManifest>, tonic::Status> {
        // An empty name means "let it choose", which is what a person pressing
        // Record means. Which config a recording was made under is the rig's
        // to say, not a caller's.
        let request = request.into_inner();
        let recorder = &self.state.recorder;
        let chosen = if request.name.is_empty() {
            recorder.suggest_a_name()
        } else {
            request.name.clone()
        };
        let loaded = self
            .state
            .loaded_config
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .map(|config| config.name.clone());
        let manifest = recorder
            .start(&chosen, &request.description, loaded.as_deref())
            .map_err(refused)?;
        self.a_recording_entry(
            KIND_RECORDING_STARTED,
            &json!(chosen),
            Some((
                "state_machine_config",
                manifest["state_machine_config"].clone(),
            )),
        );
        let manifest = recorder.manifest_of(&chosen).map_err(refused)?;
        Ok(tonic::Response::new(manifest_to_wire(&manifest)))
    }

    async fn pause(
        &self,
        _request: tonic::Request<wire::PauseRecordingRequest>,
    ) -> Result<tonic::Response<wire::RecordingManifest>, tonic::Status> {
        // A real gap in the entry numbers, recorded as a segment boundary
        // rather than smoothed over. Traced *before* the segment closes.
        let recorder = &self.state.recorder;
        let name = recorder
            .manifest_of_the_active_recording()
            .map_err(refused)?["name"]
            .clone();
        self.a_recording_entry(KIND_RECORDING_PAUSED, &name, None);
        Ok(tonic::Response::new(manifest_to_wire(
            &recorder.pause().map_err(refused)?,
        )))
    }

    async fn resume(
        &self,
        _request: tonic::Request<wire::ResumeRecordingRequest>,
    ) -> Result<tonic::Response<wire::RecordingManifest>, tonic::Status> {
        let recorder = &self.state.recorder;
        let manifest = recorder.resume().map_err(refused)?;
        self.a_recording_entry(KIND_RECORDING_RESUMED, &manifest["name"], None);
        let name = manifest["name"].as_str().unwrap_or_default();
        Ok(tonic::Response::new(manifest_to_wire(
            &recorder.manifest_of(name).map_err(refused)?,
        )))
    }

    async fn stop(
        &self,
        _request: tonic::Request<wire::StopRecordingRequest>,
    ) -> Result<tonic::Response<wire::RecordingManifest>, tonic::Status> {
        let recorder = &self.state.recorder;
        let name = recorder
            .manifest_of_the_active_recording()
            .map_err(refused)?["name"]
            .clone();
        self.a_recording_entry(KIND_RECORDING_STOPPED, &name, None);
        Ok(tonic::Response::new(manifest_to_wire(
            &recorder.stop().map_err(refused)?,
        )))
    }

    async fn clear(
        &self,
        _request: tonic::Request<wire::ClearRecordingRequest>,
    ) -> Result<tonic::Response<wire::RecordingManifest>, tonic::Status> {
        // After the clear, so a cleared recording's first line says what it
        // is: an empty file with no explanation is one somebody has to guess.
        let recorder = &self.state.recorder;
        let manifest = recorder.clear().map_err(refused)?;
        self.a_recording_entry(KIND_RECORDING_CLEARED, &manifest["name"], None);
        let name = manifest["name"].as_str().unwrap_or_default();
        Ok(tonic::Response::new(manifest_to_wire(
            &recorder.manifest_of(name).map_err(refused)?,
        )))
    }

    async fn read_recording(
        &self,
        request: tonic::Request<wire::RecordingName>,
    ) -> Result<tonic::Response<wire::RecordingManifest>, tonic::Status> {
        let name = request.into_inner().name;
        let manifest = self.state.recorder.manifest_of(&name).map_err(refused)?;
        Ok(tonic::Response::new(manifest_to_wire(&manifest)))
    }

    async fn read_entries(
        &self,
        request: tonic::Request<wire::ReadRecordingEntriesRequest>,
    ) -> Result<tonic::Response<wire::RecordingEntries>, tonic::Status> {
        // A page **by position in the file**, because of the gap above. Each
        // entry still carries its `entry_number`, so a reader can join it back
        // to the trace exactly.
        let request = request.into_inner();
        let recorder = &self.state.recorder;
        let manifest = recorder.manifest_of(&request.name).map_err(refused)?;
        let limit = if request.limit > 0 {
            request.limit as usize
        } else {
            500
        };
        let entries = recorder
            .entries_of(&request.name, request.offset.max(0) as usize, limit)
            .map_err(refused)?;
        Ok(tonic::Response::new(wire::RecordingEntries {
            name: request.name,
            offset: request.offset,
            entries: entries
                .iter()
                .filter_map(Value::as_object)
                .map(trace::trace_entry_to_wire)
                .collect(),
            entry_count: manifest
                .get("entry_count")
                .and_then(Value::as_i64)
                .unwrap_or(0),
            segments: segments_of(&manifest),
        }))
    }

    async fn delete_recording(
        &self,
        request: tonic::Request<wire::RecordingName>,
    ) -> Result<tonic::Response<wire::Recordings>, tonic::Status> {
        self.state
            .recorder
            .delete(&request.into_inner().name)
            .map_err(refused)?;
        Ok(tonic::Response::new(self.recordings()))
    }
}
