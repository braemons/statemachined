// SPDX-License-Identifier: AGPL-3.0-or-later
//! A recording: a named, deliberate selection out of the always-on trace.
//!
//! The trace is always on and always bounded — the right design for a
//! *diagnostic*, and the wrong one for a *record of an experiment*, which needs
//! a name, a boundary somebody chose, and a file that is only this run. So a
//! recording is a sink on the trace: it sees every entry **in order and none
//! skipped**, which the ring cannot promise a reader that fell behind.
//!
//! **Four verbs, and they mean four different things.** *start* opens a
//! recording and its file. *pause* stops capturing without ending it — the
//! trace keeps running, so a pause leaves a **gap that is written down** in the
//! segments. *stop* ends it; the file stays. *clear* throws away what was
//! captured **and keeps recording** — "the last ten minutes were me testing a
//! valve".
//!
//! **Two files.** `<name>.ndjson` is the entries, appended on arrival; a crash
//! otherwise loses exactly the window that mattered. `<name>.recording.json` is
//! the manifest, rewritten only by the verbs. Both are written as Python's
//! `json.dumps` writes them, so a recording does not change shape because the
//! daemon that made it did.
//!
//! **Nothing here calls back into the trace.** The trace calls this under its
//! own lock; the lifecycle entries are written by the daemon, outside both.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde_json::{json, Map, Value};

use crate::device::device_clock_correlation::{iso8601_utc, utc_of};
use crate::device::state_visit_trace::{ascii_only, TraceEntry};

pub const ENTRIES_SUFFIX: &str = ".ndjson";
pub const MANIFEST_SUFFIX: &str = ".recording.json";

pub const STATE_RECORDING: &str = "recording";
pub const STATE_PAUSED: &str = "paused";
pub const STATE_STOPPED: &str = "stopped";

/// A manifest: a JSON object, in the key order Python writes it.
pub type Manifest = Map<String, Value>;

/// Why a verb was refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecordingProblem {
    /// A name that nothing on this rig answers to.
    NotInStore(String),
    /// Refusing to record over a recording that already exists.
    NameTaken(String),
    /// The verb does not apply to what is happening.
    StateRefused(String),
    /// A different recording is in the way, and is named.
    AlreadyOpen(String),
    /// The recording named is the one being written to right now.
    InProgress(String),
    /// A name that is not a name, which here also means not a file name.
    BadName(String),
}

impl std::fmt::Display for RecordingProblem {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotInStore(s)
            | Self::NameTaken(s)
            | Self::StateRefused(s)
            | Self::AlreadyOpen(s)
            | Self::InProgress(s)
            | Self::BadName(s) => f.write_str(s),
        }
    }
}

type Recorded<T> = Result<T, RecordingProblem>;

/// The active recording, the stored ones, and the sink that fills them.
pub struct EventRecorder {
    directory: Option<PathBuf>,
    /// The manifest of the recording being written. Held in memory because it
    /// changes per entry; the file is rewritten only by the verbs.
    active: Mutex<Option<Manifest>>,
}

impl EventRecorder {
    pub fn new(directory: Option<PathBuf>) -> Self {
        Self {
            directory,
            active: Mutex::new(None),
        }
    }

    fn active_lock(&self) -> std::sync::MutexGuard<'_, Option<Manifest>> {
        self.active
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    // -- the sink ------------------------------------------------------------------

    /// One trace entry into the active recording, if one is capturing.
    pub fn record(&self, entry: &TraceEntry) {
        let mut active = self.active_lock();
        let Some(manifest) = active.as_mut() else {
            return;
        };
        if manifest["state"] != STATE_RECORDING {
            return;
        }
        let number = entry.get("entry_number").cloned().unwrap_or(Value::Null);
        if let Some(segment) = manifest["segments"]
            .as_array_mut()
            .and_then(|s| s.last_mut())
        {
            if segment["from_entry_number"].is_null() {
                segment["from_entry_number"] = number.clone();
                segment["started_host_time"] = entry
                    .get("recorded_host_time")
                    .cloned()
                    .unwrap_or(Value::Null);
            }
            segment["to_entry_number"] = number;
            segment["entry_count"] = json!(segment["entry_count"].as_i64().unwrap_or(0) + 1);
        }
        manifest["entry_count"] = json!(manifest["entry_count"].as_i64().unwrap_or(0) + 1);
        let kind = entry
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if let Some(counts) = manifest["kind_counts"].as_object_mut() {
            let count = counts.get(&kind).and_then(Value::as_i64).unwrap_or(0) + 1;
            counts.insert(kind, json!(count));
        }
        let name = manifest["name"].as_str().unwrap_or_default().to_string();
        if let Err(problem) = self.append_entry(&name, entry) {
            // A full disk must not stop a session, and a recording with a hole
            // in it that says so is usable where one that does not is a lie.
            manifest.insert("write_failure".into(), json!(problem.to_string()));
        }
    }

    // -- the verbs -----------------------------------------------------------------

    /// Open a recording. Refused if one is already open, or the name is taken:
    /// silently continuing the old one or overwriting the file is how a
    /// morning's recording turns out to be an afternoon's.
    pub fn start(
        &self,
        name: &str,
        description: &str,
        state_machine_config: Option<&str>,
    ) -> Recorded<Manifest> {
        refuse_a_name_that_is_not_one(name)?;
        let mut active = self.active_lock();
        if let Some(open) = active.as_ref() {
            return Err(RecordingProblem::AlreadyOpen(format!(
                "'{}' is already {}. Stop it before starting another.",
                open["name"].as_str().unwrap_or_default(),
                open["state"].as_str().unwrap_or_default()
            )));
        }
        if self.manifest_path(name).is_some_and(|path| path.exists()) {
            return Err(RecordingProblem::NameTaken(format!(
                "a recording called '{name}' is already stored on this rig. Delete it or \
                 choose another name."
            )));
        }
        let now = unix_seconds_now();
        let manifest = object(json!({
            "name": name,
            "description": description,
            "state_machine_config": state_machine_config,
            "created_unix_seconds": now,
            "created_host_time": iso8601_utc(now),
            "state": STATE_RECORDING,
            "segments": [a_new_segment()],
            "entry_count": 0,
            "kind_counts": {},
        }));
        self.truncate_entries(name);
        self.write_manifest(&manifest);
        *active = Some(manifest.clone());
        Ok(manifest)
    }

    /// Stop capturing; keep the recording open. The trace keeps running.
    pub fn pause(&self) -> Recorded<Manifest> {
        let mut active = self.active_lock();
        let manifest = require_active(&mut active)?;
        if manifest["state"] != STATE_RECORDING {
            return Err(RecordingProblem::StateRefused(format!(
                "'{}' is already paused",
                manifest["name"].as_str().unwrap_or_default()
            )));
        }
        manifest["state"] = json!(STATE_PAUSED);
        close_the_open_segment(manifest);
        let manifest = manifest.clone();
        self.write_manifest(&manifest);
        Ok(manifest)
    }

    pub fn resume(&self) -> Recorded<Manifest> {
        let mut active = self.active_lock();
        let manifest = require_active(&mut active)?;
        if manifest["state"] != STATE_PAUSED {
            return Err(RecordingProblem::StateRefused(format!(
                "'{}' is not paused",
                manifest["name"].as_str().unwrap_or_default()
            )));
        }
        manifest["state"] = json!(STATE_RECORDING);
        if let Some(segments) = manifest["segments"].as_array_mut() {
            segments.push(a_new_segment());
        }
        let manifest = manifest.clone();
        self.write_manifest(&manifest);
        Ok(manifest)
    }

    /// End the recording. The file stays and the recording is in the store.
    pub fn stop(&self) -> Recorded<Manifest> {
        let mut active = self.active_lock();
        let manifest = require_active(&mut active)?;
        manifest["state"] = json!(STATE_STOPPED);
        close_the_open_segment(manifest);
        manifest.insert(
            "stopped_host_time".into(),
            json!(iso8601_utc(unix_seconds_now())),
        );
        let manifest = manifest.clone();
        self.write_manifest(&manifest);
        *active = None;
        Ok(manifest)
    }

    /// Throw away what has been captured, and keep the recording open — in
    /// the state it was: clearing a paused recording leaves it paused.
    pub fn clear(&self) -> Recorded<Manifest> {
        let mut active = self.active_lock();
        let manifest = require_active(&mut active)?;
        let segments = if manifest["state"] == STATE_RECORDING {
            json!([a_new_segment()])
        } else {
            json!([])
        };
        manifest["segments"] = segments;
        manifest["entry_count"] = json!(0);
        manifest["kind_counts"] = json!({});
        manifest.insert(
            "cleared_host_time".into(),
            json!(iso8601_utc(unix_seconds_now())),
        );
        let manifest = manifest.clone();
        let name = manifest["name"].as_str().unwrap_or_default().to_string();
        self.truncate_entries(&name);
        self.write_manifest(&manifest);
        Ok(manifest)
    }

    // -- the store -----------------------------------------------------------------

    pub fn active(&self) -> Option<Manifest> {
        self.active_lock().clone()
    }

    /// The open recording, or a refusal naming the verb that would open one.
    pub fn manifest_of_the_active_recording(&self) -> Recorded<Manifest> {
        let mut active = self.active_lock();
        Ok(require_active(&mut active)?.clone())
    }

    /// Every recording on this rig, newest first, unreadable ones included
    /// with their reason: a file you cannot see is a file you cannot fix.
    pub fn manifests(&self) -> Vec<Manifest> {
        let Some(directory) = &self.directory else {
            return Vec::new();
        };
        let Ok(listing) = std::fs::read_dir(directory) else {
            return Vec::new();
        };
        let mut names: Vec<String> = listing
            .filter_map(|entry| entry.ok())
            .filter_map(|entry| entry.file_name().into_string().ok())
            .filter_map(|file| file.strip_suffix(MANIFEST_SUFFIX).map(str::to_string))
            .collect();
        names.sort();
        let mut found: Vec<Manifest> = names
            .iter()
            .map(|name| {
                self.manifest_of(name).unwrap_or_else(|problem| {
                    object(json!({ "name": name, "unreadable": problem.to_string() }))
                })
            })
            .collect();
        // Stable, as Python's `sort` is: equal times stay in name order.
        found.sort_by(|a, b| {
            let created = |m: &Manifest| {
                m.get("created_unix_seconds")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0)
            };
            created(b).total_cmp(&created(a))
        });
        found
    }

    /// One recording's manifest — the live one if it is being recorded, since
    /// its counts are only in memory between the verbs.
    pub fn manifest_of(&self, name: &str) -> Recorded<Manifest> {
        refuse_a_name_that_is_not_one(name)?;
        if let Some(active) = self.active_lock().as_ref() {
            if active["name"] == name {
                return Ok(active.clone());
            }
        }
        let path = self
            .manifest_path(name)
            .filter(|path| path.exists())
            .ok_or_else(|| {
                RecordingProblem::NotInStore(format!("this rig has no recording called '{name}'"))
            })?;
        let text = std::fs::read_to_string(&path)
            .map_err(|problem| RecordingProblem::NotInStore(problem.to_string()))?;
        let mut manifest: Manifest = serde_json::from_str(&text)
            .map_err(|problem| RecordingProblem::NotInStore(problem.to_string()))?;
        manifest.insert("entries_bytes".into(), json!(self.entries_bytes(name)));
        Ok(manifest)
    }

    /// Entries read back out of the file, by position in it: a recording with
    /// a gap has no contiguous range of entry numbers to slice. Each entry
    /// still carries its `entry_number`, so joining to the trace is exact.
    pub fn entries_of(&self, name: &str, offset: usize, limit: usize) -> Recorded<Vec<Value>> {
        refuse_a_name_that_is_not_one(name)?;
        let Some(path) = self.entries_path(name).filter(|path| path.exists()) else {
            if self.manifest_path(name).is_some_and(|path| path.exists()) {
                return Ok(Vec::new());
            }
            return Err(RecordingProblem::NotInStore(format!(
                "this rig has no recording called '{name}'"
            )));
        };
        let text = std::fs::read_to_string(path)
            .map_err(|problem| RecordingProblem::NotInStore(problem.to_string()))?;
        Ok(text
            .lines()
            .skip(offset)
            .take(limit)
            .map(str::trim)
            .filter(|line| !line.is_empty())
            .filter_map(|line| serde_json::from_str(line).ok())
            .collect())
    }

    /// Remove a stored recording. Refused while it is the one being written:
    /// deleting the file under it would leave the rig writing into nothing.
    pub fn delete(&self, name: &str) -> Recorded<()> {
        refuse_a_name_that_is_not_one(name)?;
        if self
            .active_lock()
            .as_ref()
            .is_some_and(|active| active["name"] == name)
        {
            return Err(RecordingProblem::InProgress(format!(
                "'{name}' is being recorded right now. Stop it first -- deleting the file \
                 under a running recording would leave the rig writing into nothing."
            )));
        }
        let manifest_path = self
            .manifest_path(name)
            .filter(|path| path.exists())
            .ok_or_else(|| {
                RecordingProblem::NotInStore(format!("this rig has no recording called '{name}'"))
            })?;
        let _ = std::fs::remove_file(manifest_path);
        if let Some(entries) = self.entries_path(name).filter(|path| path.exists()) {
            let _ = std::fs::remove_file(entries);
        }
        Ok(())
    }

    /// A name for somebody who did not bring one: the time it started.
    pub fn suggest_a_name(&self) -> String {
        let stamp = utc_of(unix_seconds_now().trunc() as i64);
        // `YYYY-MM-DDTHH:MM:SS` into `recording-YYYYMMDD-HHMMSS`.
        let digits: String = stamp.chars().filter(char::is_ascii_digit).collect();
        format!("recording-{}-{}", &digits[..8], &digits[8..14])
    }

    // -- the files -------------------------------------------------------------------

    fn manifest_path(&self, name: &str) -> Option<PathBuf> {
        self.directory
            .as_ref()
            .map(|d| d.join(format!("{name}{MANIFEST_SUFFIX}")))
    }

    fn entries_path(&self, name: &str) -> Option<PathBuf> {
        self.directory
            .as_ref()
            .map(|d| d.join(format!("{name}{ENTRIES_SUFFIX}")))
    }

    fn entries_bytes(&self, name: &str) -> u64 {
        self.entries_path(name)
            .and_then(|path| std::fs::metadata(path).ok())
            .map(|metadata| metadata.len())
            .unwrap_or(0)
    }

    fn append_entry(&self, name: &str, entry: &TraceEntry) -> std::io::Result<()> {
        let (Some(directory), Some(path)) = (&self.directory, self.entries_path(name)) else {
            return Ok(());
        };
        std::fs::create_dir_all(directory)?;
        let line = ascii_only(&serde_json::to_string(entry).expect("an entry serialises"));
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)?
            .write_all(format!("{line}\n").as_bytes())
    }

    fn truncate_entries(&self, name: &str) {
        let (Some(directory), Some(path)) = (&self.directory, self.entries_path(name)) else {
            return;
        };
        let _ = std::fs::create_dir_all(directory).and_then(|()| std::fs::write(path, ""));
    }

    /// Written aside and renamed, so a crash mid-write leaves the old manifest
    /// rather than half of a new one.
    fn write_manifest(&self, manifest: &Manifest) {
        let name = manifest["name"].as_str().unwrap_or_default();
        let (Some(directory), Some(path)) = (&self.directory, self.manifest_path(name)) else {
            return;
        };
        let text = python_indented(&Value::Object(manifest.clone()));
        let _ = (|| -> std::io::Result<()> {
            std::fs::create_dir_all(directory)?;
            let temporary = with_suffix_replaced(&path, ".writing");
            std::fs::write(&temporary, format!("{text}\n"))?;
            std::fs::rename(temporary, path)
        })();
    }
}

/// `json.dumps(value, indent=2)`: two spaces, `": "`, and non-ASCII escaped.
pub fn python_indented(value: &Value) -> String {
    ascii_only(&serde_json::to_string_pretty(value).expect("a manifest serialises"))
}

/// `Path.with_suffix`: the last suffix replaced. `x.recording.json` becomes
/// `x.recording.writing`, as Python names it.
fn with_suffix_replaced(path: &Path, suffix: &str) -> PathBuf {
    path.with_extension(suffix.trim_start_matches('.'))
}

fn require_active(active: &mut Option<Manifest>) -> Recorded<&mut Manifest> {
    active.as_mut().ok_or_else(|| {
        RecordingProblem::StateRefused(
            "no recording is open on this rig. Start one, or -- to remove a stored recording \
             -- delete it by name."
                .into(),
        )
    })
}

/// Close the open segment; one that caught nothing is dropped rather than
/// stored as a pair of nulls — a reader counting gaps should not have to
/// filter out a pause somebody took immediately.
fn close_the_open_segment(manifest: &mut Manifest) {
    let Some(segments) = manifest["segments"].as_array_mut() else {
        return;
    };
    let Some(segment) = segments.last_mut() else {
        return;
    };
    if segment["ended_host_time"].is_null() {
        segment["ended_host_time"] = json!(iso8601_utc(unix_seconds_now()));
    }
    if segment["from_entry_number"].is_null() {
        segments.pop();
    }
}

fn a_new_segment() -> Value {
    json!({
        "from_entry_number": null,
        "to_entry_number": null,
        "started_host_time": null,
        "ended_host_time": null,
        "entry_count": 0,
    })
}

/// It becomes a file name, so it starts with a letter or digit and holds only
/// letters, digits, dot, dash and underscore — which is also what keeps a `/`
/// from making it a path.
fn refuse_a_name_that_is_not_one(name: &str) -> Recorded<()> {
    let mut characters = name.chars();
    let usable = characters
        .next()
        .is_some_and(|first| first.is_ascii_alphanumeric())
        && characters.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'));
    if usable {
        return Ok(());
    }
    Err(RecordingProblem::BadName(format!(
        "'{name}' is not a usable recording name. It becomes a file name, so it must start \
         with a letter or digit and hold only letters, digits, dot, dash and underscore."
    )))
}

fn object(value: Value) -> Manifest {
    match value {
        Value::Object(map) => map,
        _ => Manifest::new(),
    }
}

fn unix_seconds_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|since| since.as_secs_f64())
        .unwrap_or(0.0)
}
