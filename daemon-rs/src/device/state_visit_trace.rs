// SPDX-License-Identifier: AGPL-3.0-or-later
//! The trace: every state the machine entered, timestamped, kept regardless.
//!
//! This is the one job that is *only* possible in a daemon: it has to be
//! running and listening at the moment a state is entered, which no library
//! invoked per trial is.
//!
//! **A ring in memory is what the API reads.** That is what makes the rest of
//! this small: there is no "which file holds trial 193", no seek, no index. A
//! query is a scan of a deque.
//!
//! **And a file it never reads back.** The ring is volatile and a restart
//! during a package upgrade must not silently cost the morning's traces, so
//! each entry is also appended to NDJSON — one file per day, rotated by
//! logrotate. Appended **on arrival, not on eviction**: a crash otherwise loses
//! exactly the window that mattered most, which is everything still in the
//! ring.
//!
//! **This is not the `.tdr` and must not grow into one.** triald writes the
//! trial record. This is finer grained — one line per state visit — and it
//! joins to the `.tdr` on `trial_id`. Nothing in it is a verdict.
//!
//! An entry is a JSON object rather than a struct, as it is in Python, because
//! **the set of kinds grows with the firmware**: a daemon must be able to carry
//! an entry from a board newer than itself.

use std::collections::VecDeque;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

use serde_json::{Map, Value};

use super::device_clock_correlation::{iso8601_utc, utc_of};

/// One entry: `entry_number`, `kind`, `recorded_host_time`, then whatever the
/// kind carries, in that order.
pub type TraceEntry = Map<String, Value>;

/// Called with every entry as it is appended.
pub type Sink = Box<dyn Fn(&TraceEntry) + Send>;

// Entry kinds. The device is not the only thing worth timestamping: aligning
// an external signal to trial 193 needs to know when trial 193 was *armed*,
// not only which states it visited.
pub const KIND_STATE_VISIT: &str = "visit";
pub const KIND_TRIAL_CONFIGURED: &str = "trial_configured";
pub const KIND_TRIAL_STARTED: &str = "trial_started";
pub const KIND_TRIAL_CANCELLED: &str = "trial_cancelled";
pub const KIND_TRIAL_RESULT: &str = "trial_result";
pub const KIND_GRAPH_SET_UPLOADED: &str = "graph_set_uploaded";
/// A state-machine config was applied: the line map changed, and with it what
/// every graph's names mean.
pub const KIND_CONFIG_LOADED: &str = "state_machine_config_loaded";
/// A session opened or closed. Not the device's business, but it is the
/// boundary an analysis cuts on.
pub const KIND_SESSION_OPENED: &str = "session_opened";
pub const KIND_SESSION_CLOSED: &str = "session_closed";
/// Which graph a trial gets when nobody names one. Nothing is pushed, but it
/// changes what the next `Configure` means, so it is written down.
pub const KIND_ACTIVE_GRAPH_SELECTED: &str = "active_graph_selected";
/// The board was handed the job of arming its own trials, or had it taken
/// back: the answer to "who started trial 412".
pub const KIND_AUTORUN_CHANGED: &str = "autorun_changed";
/// The board's settings were written to its own storage — a change to what it
/// will be after the next power cut.
pub const KIND_SETTINGS_SAVED: &str = "settings_saved";
pub const KIND_RECORDING_STARTED: &str = "recording_started";
pub const KIND_RECORDING_PAUSED: &str = "recording_paused";
pub const KIND_RECORDING_RESUMED: &str = "recording_resumed";
pub const KIND_RECORDING_STOPPED: &str = "recording_stopped";
pub const KIND_RECORDING_CLEARED: &str = "recording_cleared";
pub const KIND_LINK_CONNECTED: &str = "link_connected";
pub const KIND_LINK_LOST: &str = "link_lost";
pub const KIND_SEQUENCE_GAP: &str = "sequence_gap";

struct Held {
    entries: VecDeque<TraceEntry>,
    /// Monotonic for the life of the daemon. The device's `seq` counts visits
    /// within a *run* and restarts every trial, so it cannot address a
    /// position in a log that spans a session — which is what a `since_`
    /// cursor needs.
    next_entry_number: i64,
    /// A sink sees entries **in order and none skipped**, which is what the
    /// ring cannot promise a reader that fell behind — and why a recording is
    /// a sink rather than a poller.
    sinks: Vec<Sink>,
}

/// The ring, the file, and the entry numbers that address them.
///
/// Written from whichever thread owns the link and read from the rpcs. One
/// lock, held across a push and a file write — both cheap, and neither able to
/// block on the device.
pub struct StateVisitTrace {
    held: Mutex<Held>,
    ring_capacity: usize,
    directory: Option<PathBuf>,
}

impl StateVisitTrace {
    pub fn new(ring_entries: usize, directory: Option<PathBuf>) -> Self {
        Self {
            held: Mutex::new(Held {
                entries: VecDeque::with_capacity(ring_entries.min(65_536)),
                next_entry_number: 0,
                sinks: Vec::new(),
            }),
            ring_capacity: ring_entries,
            directory,
        }
    }

    fn held(&self) -> std::sync::MutexGuard<'_, Held> {
        // A sink that panicked mid-append leaves the ring as it was before the
        // push or after it, both of which are a ring. Carrying on beats taking
        // the trace down with it.
        self.held
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub fn ring_capacity(&self) -> usize {
        self.ring_capacity
    }

    /// Send every future entry to `sink` as well as to the ring.
    ///
    /// Called under the trace's lock, so a sink must be quick and must not
    /// call back into the trace — appending from inside a sink would deadlock.
    pub fn add_sink(&self, sink: Sink) {
        self.held().sinks.push(sink);
    }

    /// Add one entry, to the ring and to the day's file, and return it.
    ///
    /// `recorded_host_time` is the daemon's own clock and is *not* an
    /// estimate of the device's, unlike a visit's `entered_host_time`.
    pub fn append(&self, kind: &str, fields: TraceEntry) -> TraceEntry {
        let mut held = self.held();
        let now = unix_seconds_now();
        let mut entry = TraceEntry::new();
        entry.insert("entry_number".into(), Value::from(held.next_entry_number));
        entry.insert("kind".into(), Value::from(kind));
        entry.insert("recorded_host_time".into(), Value::from(iso8601_utc(now)));
        // Python's `{**named, **fields}`: a field may not rename the three
        // above, but it may overwrite them, and does so in place.
        for (key, value) in fields {
            entry.insert(key, value);
        }
        held.next_entry_number += 1;
        if self.ring_capacity == 0 {
            // `deque(maxlen=0)` holds nothing, and still numbers and writes.
        } else {
            if held.entries.len() == self.ring_capacity {
                held.entries.pop_front();
            }
            held.entries.push_back(entry.clone());
        }
        self.append_to_todays_file(&entry, now);
        for sink in &held.sinks {
            // Same rule as the day's file: a sink that cannot write must not
            // stop a session.
            let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| sink(&entry)));
        }
        entry
    }

    fn append_to_todays_file(&self, entry: &TraceEntry, now: f64) {
        let Some(directory) = &self.directory else {
            return;
        };
        // A full or read-only disk must not stop a session. The ring is what
        // the API reads, so the trace survives this; what is lost is the
        // durable copy, and losing a session over it would be worse.
        let _ = (|| -> std::io::Result<()> {
            std::fs::create_dir_all(directory)?;
            let day = &utc_of(now.trunc() as i64)[..10];
            let mut file = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(directory.join(format!("trace-{day}.ndjson")))?;
            let line = ascii_only(&serde_json::to_string(entry).expect("an entry serialises"));
            file.write_all(format!("{line}\n").as_bytes())
        })();
    }

    // -- queries ---------------------------------------------------------------

    pub fn entries_since(&self, entry_number: i64, limit: usize) -> Vec<TraceEntry> {
        self.held()
            .entries
            .iter()
            .filter(|entry| number_of(entry) >= entry_number)
            .take(limit)
            .cloned()
            .collect()
    }

    pub fn entries_for_trial(&self, trial_id: i64) -> Vec<TraceEntry> {
        self.held()
            .entries
            .iter()
            .filter(|entry| entry.get("trial_id").and_then(Value::as_i64) == Some(trial_id))
            .cloned()
            .collect()
    }

    pub fn newest_entry_number(&self) -> i64 {
        self.held().next_entry_number - 1
    }

    pub fn oldest_entry_number_still_held(&self) -> i64 {
        let held = self.held();
        held.entries
            .front()
            .map(number_of)
            .unwrap_or(held.next_entry_number)
    }

    /// Whether a cursor is older than anything still held.
    ///
    /// A consumer slow enough for this has genuinely lost data and must be
    /// **told so**, rather than handed a shorter answer that looks complete.
    /// It is the one place the ring's boundedness is visible from outside.
    pub fn has_fallen_out_of_the_ring(&self, entry_number: i64) -> bool {
        self.held()
            .entries
            .front()
            .is_some_and(|oldest| entry_number < number_of(oldest))
    }

    pub fn len(&self) -> usize {
        self.held().entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// JSON as Python's `json.dumps` writes it: every non-ASCII character as a
/// `\uXXXX` escape, and one past the BMP as a surrogate pair.
///
/// So a day's file is the same bytes whichever daemon wrote it. Safe to apply
/// to the whole line because JSON has non-ASCII nowhere but inside strings.
pub fn ascii_only(json: &str) -> String {
    let mut out = String::with_capacity(json.len());
    for character in json.chars() {
        if character.is_ascii() {
            out.push(character);
        } else {
            let mut units = [0u16; 2];
            for unit in character.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

fn number_of(entry: &TraceEntry) -> i64 {
    entry
        .get("entry_number")
        .and_then(Value::as_i64)
        .unwrap_or_default()
}

fn unix_seconds_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("a clock after 1970")
        .as_secs_f64()
}

/// Fields for an entry, as `json!` spells them.
pub fn fields(object: Value) -> TraceEntry {
    match object {
        Value::Object(fields) => fields,
        _ => TraceEntry::new(),
    }
}
