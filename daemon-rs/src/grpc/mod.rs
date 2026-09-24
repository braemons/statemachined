// SPDX-License-Identifier: AGPL-3.0-or-later
//! The rpcs, as the generated traits ask for them.
//!
//! **This is the port's scoreboard.** Every rpc in `proto/` appears here
//! because the trait requires it — an rpc with no implementation does not
//! compile — and the ones not yet ported answer `UNIMPLEMENTED` rather than
//! being absent. A client asking for one gets the same answer it would get
//! from a daemon that never had it, which is the honest thing for a daemon
//! that is half-ported, and `make ported` counts what is left.
//!
//! Each rpc moves from `unported!()` to a real body exactly once, and the
//! Python daemon in `daemon/` stays the one on rigs until every one of them
//! has (`dev/RUST_PORT.md` §5.2: there is no split where both run).

use std::sync::Arc;

use crate::daemon_state::DaemonState;
use crate::device::device_pin_map::PinLabelSource;
use crate::device::statemachined_device::{DeviceProblem, StatemachinedDevice};
use crate::rig_configuration::RigConfiguration;
use crate::device::state_visit_trace::{
    fields as trace_fields, KIND_ACTIVE_GRAPH_SELECTED, KIND_CONFIG_LOADED,
    KIND_GRAPH_SET_UPLOADED, KIND_LINK_CONNECTED, KIND_SESSION_CLOSED, KIND_SESSION_OPENED,
};
use crate::device::statemachined_device::UploadProblem;
use crate::firmware_manifest::{compare_firmware, installed_firmware_version, INSTALLED_MANIFEST};
use crate::graph_set_compiler::{compile_graph_set_for_device, CompiledGraphSet, DeviceCapabilities};
use crate::model::graph_definition::GraphDefinition;
use crate::model::state_machine_config::StateMachineConfig;
use crate::store::{Document, Store, StoreProblem};
use crate::wire::statemachined::v1 as wire;

use crate::wire::statemachined::v1::service;

mod refusal;
mod trace;
mod trial;
use refusal::{store_refusal, Category, Refusal, StoreKind};

/// Not ported yet. See the module docstring.
///
/// A macro rather than a function so that the rpc's own name reaches the
/// message: "no such rpc" and "that rpc exists but this build cannot do it
/// yet" are different problems and a person reading a log needs to tell them
/// apart.
macro_rules! unported {
    ($name:literal) => {
        Err(tonic::Status::unimplemented(concat!(
            $name,
            " is not ported to the Rust daemon yet; the Python daemon answers it"
        )))
    };
}

/// Every service, over one shared state. The same object each trait is
/// implemented for, so there is one daemon rather than eight.
#[derive(Clone)]
pub struct DaemonServices {
    state: Arc<DaemonState>,
}

impl DaemonServices {
    pub fn new(state: Arc<DaemonState>) -> Self {
        Self { state }
    }
}

/// A store's failure, as the status a client sees.
///
/// **The category is the daemon's own, not an HTTP status.** "no graph called
/// that is stored" is `not_found` and "this file does not parse" is
/// `invalid_argument`, and a caller that can tell them apart can retry one and
/// not the other. The Python daemon draws the same line in
/// `servicers/refusals.py`.
/// A lock whose holder panicked.
///
/// Answered rather than propagated: one rpc that panicked mid-request should
/// not make every later one panic too, and `internal` is the honest word for
/// "this daemon is in a state it does not understand".
fn poisoned<T>(_: std::sync::PoisonError<T>) -> tonic::Status {
    tonic::Status::internal("the device lock was left poisoned by a failed request")
}

fn graph_problem(problem: StoreProblem) -> tonic::Status {
    store_refusal(StoreKind::Graph, problem).into()
}

fn config_problem(problem: StoreProblem) -> tonic::Status {
    store_refusal(StoreKind::StateMachineConfig, problem).into()
}

/// One line about a stored graph, for a listing.
///
/// **A graph that does not parse still appears**, with `readable` false and the
/// reason in `detail`. A listing that silently omitted a broken file would
/// leave somebody looking at a directory that has a graph in it and a UI that
/// says it has not.
fn graph_summary(store: &Store<GraphDefinition>, name: &str) -> wire::GraphSummary {
    match store.load(name) {
        Ok(graph) => wire::GraphSummary {
            name: name.to_string(),
            readable: true,
            detail: String::new(),
            state_count: graph.states.len() as i32,
            entry: graph.entry,
        },
        Err(problem) => wire::GraphSummary {
            name: name.to_string(),
            readable: false,
            detail: problem.to_string(),
            state_count: 0,
            entry: String::new(),
        },
    }
}

fn graph_summaries(store: &Store<GraphDefinition>) -> wire::GraphSummaries {
    wire::GraphSummaries {
        graphs: store
            .stored_names()
            .iter()
            .map(|name| graph_summary(store, name))
            .collect(),
    }
}

fn config_summary(
    store: &Store<StateMachineConfig>,
    name: &str,
) -> wire::StateMachineConfigSummary {
    match store.load(name) {
        Ok(config) => wire::StateMachineConfigSummary {
            name: name.to_string(),
            readable: true,
            detail: String::new(),
            description: config.description,
            board: config.board,
            graph_names: config.graphs.iter().map(|g| g.name.clone()).collect(),
            input_line_count: config.line_map.input_lines.len() as i32,
            output_line_count: config.line_map.output_lines.len() as i32,
        },
        Err(problem) => wire::StateMachineConfigSummary {
            name: name.to_string(),
            readable: false,
            detail: problem.to_string(),
            ..Default::default()
        },
    }
}

/// The rig configuration, as the wire spells it.
///
/// The convert seam: `rig_configuration::RigConfiguration` is what the daemon
/// thinks in and reads from TOML, and this is the message. They are nearly the
/// same shape and are still two types, because the day they diverge is the day
/// a field means something different on disk than on the wire.
fn rig_configuration_to_wire(configuration: &RigConfiguration) -> wire::RigConfiguration {
    wire::RigConfiguration {
        device_target: configuration.device_target.clone(),
        device_baud: configuration.device_baud,
        device_timeout_seconds: configuration.device_timeout_seconds,
        expected_board: configuration.expected_board.clone(),
        connect_on_startup: configuration.connect_on_startup,
        session_seed: configuration.session_seed.clone(),
        startup_state_machine_config: configuration.startup_state_machine_config.clone(),
        graph_mode: configuration.graph_mode.as_str().to_string(),
        trace_ring_entries: configuration.trace_ring_entries,
        heartbeat_seconds: configuration.heartbeat_seconds,
        trace_directory: path_to_wire(&configuration.trace_directory),
        graph_store_directory: path_to_wire(&configuration.graph_store_directory),
        recording_directory: path_to_wire(&configuration.recording_directory),
        state_machine_config_directory: path_to_wire(
            &configuration.state_machine_config_directory,
        ),
    }
}

/// A path as the wire carries it.
///
/// Lossy only for a path that is not UTF-8, which on these rigs would be a
/// directory somebody could not have typed into a TOML file either.
fn path_to_wire(path: &std::path::Path) -> String {
    path.to_string_lossy().into_owned()
}

/// Everything about the attachment, in one message.
///
/// Assembled from three sources because that is what it is: the greeting says
/// what the board *is*, the state report says what it is *doing*, and this
/// daemon holds the rest. A caller should not have to make three calls and
/// join them.
fn device_state_to_wire(
    device: &StatemachinedDevice,
    target: &str,
    state_report: &serde_json::Value,
    last_error: String,
) -> wire::DeviceState {
    let ack = device.hello_ack.clone().unwrap_or(serde_json::Value::Null);
    // Python's `_text` and `_int`: absent is empty or zero, and a number that
    // arrived as a float still counts.
    let text = |source: &serde_json::Value, key: &str| match source.get(key) {
        None | Some(serde_json::Value::Null) => String::new(),
        Some(serde_json::Value::String(text)) => text.clone(),
        Some(other) => other.to_string(),
    };
    let number = |source: &serde_json::Value, key: &str| {
        source
            .get(key)
            .and_then(|value| value.as_i64().or_else(|| value.as_f64().map(|f| f as i64)))
            .unwrap_or(0)
    };
    let scan = state_report.get("scan").cloned().unwrap_or(serde_json::Value::Null);
    // Three answers, not two: the board said it has a wiring table, said it
    // has none, or nothing has said. The report is newer than the greeting.
    let has_wiring = state_report
        .get("has_wiring")
        .or_else(|| ack.get("has_wiring"))
        .filter(|value| !value.is_null())
        .map(|value| value.as_bool().unwrap_or(true));
    wire::DeviceState {
        connected: device.is_connected(),
        target: target.to_string(),
        board: text(&ack, "board"),
        firmware_version: text(&ack, "fw"),
        protocol_version: number(&ack, "proto") as i32,
        measured_scan_hz: number(&ack, "scan_hz") as i32,
        // The whole reason the `pins` command exists: before it there was no
        // third answer to "which pin is line 4" beyond a table copied out of
        // the firmware. So the provenance travels with the answer.
        pin_labels_came_from: source_of(device.pin_map.source).to_string(),
        link: Some(wire::LinkHealth {
            connection_count: device.connection_count as i32,
            dropped_lines: number(state_report, "dropped_lines"),
            bad_lines: number(state_report, "bad_lines"),
            last_error,
        }),
        scan: Some(wire::ScanHealth {
            hz: number(&scan, "hz") as i32,
            overruns: number(&scan, "overruns"),
            worst_gap: number(&scan, "worst_gap") as i32,
            tx_stalls: number(&scan, "tx_stalls"),
        }),
        uptime_device_microseconds: number(state_report, "up_us"),
        capacities: device.capabilities.as_ref().map(capacities_to_wire),
        committed_set: device.committed_graph_set.as_ref().map(committed_set_to_wire),
        has_wiring,
    }
}

/// What a set costs, or what a board holds. Six numbers, not one: the pools
/// fill independently.
fn pool_counts(counts: &indexmap::IndexMap<String, i64>) -> wire::GraphPoolCounts {
    let count = |name: &str| counts.get(name).copied().unwrap_or(0) as i32;
    wire::GraphPoolCounts {
        graphs: count("graphs"),
        states: count("states"),
        transitions: count("transitions"),
        output_actions: count("output_actions"),
        distributions: count("distributions"),
        choice_options: count("choice_options"),
    }
}

/// The graph set on the board, by the names it was compiled from.
fn committed_set_to_wire(committed: &CompiledGraphSet) -> wire::CommittedGraphSet {
    wire::CommittedGraphSet {
        set_version: committed.set_version as i32,
        graph_names: committed
            .graphs_by_slot
            .iter()
            .map(|graph| graph.name.clone())
            .collect(),
        pool_usage: Some(pool_counts(&committed.pool_usage)),
        pool_capacity: Some(pool_counts(&committed.pool_capacity)),
    }
}

/// What putting the graphs on the device cost.
///
/// `slots` is here although no caller needs it — everything else in this API
/// takes a name — because it is what a person compares against the board when
/// a trial reports the wrong graph.
fn open_session_result(
    compiled: &CompiledGraphSet,
    state_machine_config: String,
    elapsed_milliseconds: u128,
) -> wire::OpenSessionResult {
    wire::OpenSessionResult {
        state_machine_config,
        set_version: compiled.set_version as i32,
        slots: compiled
            .graphs_by_slot
            .iter()
            .map(|graph| (graph.name.clone(), graph.slot as i32))
            .collect(),
        pool_usage: Some(pool_counts(&compiled.pool_usage)),
        pool_capacity: Some(pool_counts(&compiled.pool_capacity)),
        elapsed_milliseconds: elapsed_milliseconds as i32,
    }
}

impl From<UploadProblem> for Refusal {
    fn from(problem: UploadProblem) -> Self {
        match problem {
            UploadProblem::Compile(problem) => problem.into(),
            UploadProblem::Device(problem) => problem.into(),
        }
    }
}

/// How often a watcher looks for something new. Coarse on purpose: this is a
/// console refreshing, not a control loop.
const WATCH_PERIOD: std::time::Duration = std::time::Duration::from_millis(100);

/// One stream, registered as an observer for exactly as long as it runs.
///
/// Unregistered on drop, which is the whole point: a stream ends by a
/// cancelled call, a client that went away, or a daemon shutting down, and
/// every one of those has to unregister. An observer list that only lost
/// entries on a clean close would fill with ghosts.
struct Watching {
    state: Arc<DaemonState>,
    observer_id: u64,
}

impl Watching {
    fn register<T>(state: &Arc<DaemonState>, request: &tonic::Request<T>, stream: &str) -> Self {
        // A header rather than a field on every request, because it says
        // nothing about what is being asked for.
        let name = request
            .metadata()
            .get("observer-name")
            .and_then(|value| value.to_str().ok());
        // grpcio's spelling of a peer, so a panel shows the same thing from
        // either daemon.
        // From axum's connect info, since axum and not tonic owns the socket;
        // tonic's own, where a test serves it directly.
        let peer = request
            .extensions()
            .get::<axum::extract::ConnectInfo<std::net::SocketAddr>>()
            .map(|info| info.0)
            .or_else(|| request.remote_addr());
        let address = peer.map(|address| match address {
            std::net::SocketAddr::V4(_) => format!("ipv4:{address}"),
            std::net::SocketAddr::V6(_) => format!("ipv6:{address}"),
        });
        Self {
            observer_id: state.observers.register(name, stream, address),
            state: state.clone(),
        }
    }
}

impl Drop for Watching {
    fn drop(&mut self) {
        self.state.observers.unregister(self.observer_id);
    }
}

/// Run an rpc's body off the async runtime.
///
/// **The device work is synchronous**: a serial port, a lock, and a reply
/// waited for. Run on the runtime it would stall every other rpc — the state
/// a console is watching included — for as long as a whole set took to
/// upload. Python's `answering` runs every body in `asyncio.to_thread` for the
/// same reason.
async fn off_the_runtime<T: Send + 'static>(
    work: impl FnOnce() -> Result<T, tonic::Status> + Send + 'static,
) -> Result<T, tonic::Status> {
    tokio::task::spawn_blocking(work)
        .await
        .map_err(|problem| tonic::Status::internal(format!("the request's worker failed: {problem}")))?
}

impl DaemonState {
    /// One more than the last, wrapping inside the wire's `u16`.
    ///
    /// The daemon's number, not a hash of the contents: `configure` carries it
    /// so that a set edit which did not land cannot leave the device
    /// confidently running the old paradigms, and for that it only has to
    /// *differ*.
    fn next_set_version(device: &StatemachinedDevice) -> i64 {
        let previous = device
            .committed_graph_set
            .as_ref()
            .map(|committed| committed.set_version)
            .unwrap_or(0);
        (previous % 65535) + 1
    }

    /// Compile, check against this board's caps, upload, commit — over graphs
    /// from the store, by name, in the order named, which becomes the slot
    /// order.
    ///
    /// **A session is open afterwards**, whichever rpc asked. When triald
    /// drives a rig it calls this, and a panel that only knew about
    /// `Session/Open` would show "no session" beside a board running trials.
    fn upload_session_graph_set(
        &self,
        graph_names: &[String],
    ) -> Result<(CompiledGraphSet, u128), tonic::Status> {
        let graphs = graph_names
            .iter()
            .map(|name| self.graphs.load(name))
            .collect::<Result<Vec<_>, _>>()
            .map_err(graph_problem)?;
        let (compiled, elapsed) = self.upload_graph_set(&graphs)?;
        self.note_the_session_opened(&compiled, "graph_names")?;
        Ok((compiled, elapsed))
    }

    /// Put the loaded config's graphs on the device.
    fn open_session(&self) -> Result<(StateMachineConfig, CompiledGraphSet, u128), tonic::Status> {
        let config = self.require_state_machine_config()?;
        let (compiled, elapsed) = self.upload_graph_set(&config.graphs)?;
        self.note_the_session_opened(&compiled, "state_machine_config")?;
        Ok((config, compiled, elapsed))
    }

    /// A session is open, by whichever of the two ways, and the trace says so.
    fn note_the_session_opened(
        &self,
        compiled: &CompiledGraphSet,
        opened_by: &str,
    ) -> Result<(), tonic::Status> {
        *self.session_opened_at.lock().map_err(poisoned)? = Some(std::time::SystemTime::now());
        let loaded = self.loaded_config.lock().map_err(poisoned)?.as_ref().map(|c| c.name.clone());
        self.trace.append(
            KIND_SESSION_OPENED,
            trace_fields(serde_json::json!({
                "state_machine_config": loaded,
                "set_version": compiled.set_version,
                "graph_names": graph_names_of(compiled),
                "opened_by": opened_by,
            })),
        );
        Ok(())
    }

    /// Compile, check against this board's caps, upload, commit. Returns what
    /// it cost in milliseconds — the slowest call in the API, and the one a
    /// panel shows a progress bar for.
    fn upload_graph_set(
        &self,
        graphs: &[GraphDefinition],
    ) -> Result<(CompiledGraphSet, u128), tonic::Status> {
        // Timed from before the lock, as Python times it: a caller waiting on
        // somebody else's upload is waiting on this call.
        let started = std::time::Instant::now();
        let compiled = self
            .with_device(|device| {
                let set_version = Self::next_set_version(device);
                device.upload_graph_set(graphs, set_version)
            })?
            .map_err(|problem| tonic::Status::from(Refusal::from(problem)))?;
        let elapsed_milliseconds = started.elapsed().as_millis();
        self.trace.append(
            KIND_GRAPH_SET_UPLOADED,
            trace_fields(serde_json::json!({
                "set_version": compiled.set_version,
                "graph_names": graph_names_of(&compiled),
                "elapsed_milliseconds": elapsed_milliseconds as u64,
            })),
        );
        Ok((compiled, elapsed_milliseconds))
    }

    /// The loaded config, or a refusal that lists what could be loaded.
    fn require_state_machine_config(&self) -> Result<StateMachineConfig, tonic::Status> {
        if let Some(config) = self.loaded_config.lock().map_err(poisoned)?.clone() {
            return Ok(config);
        }
        let stored = self.configs.stored_names();
        let known = if stored.is_empty() { "(none)".to_string() } else { stored.join(", ") };
        Err(Refusal::new(
            Category::WrongMoment,
            "no_state_machine_config_loaded",
            format!(
                "this rig has no state-machine config loaded, so nothing says which pin is \
                 which line or what it can run. Load one: {known}"
            ),
            "state_machine_config",
        )
        .into())
    }

    /// Make this config the rig's, and push the wiring it implies.
    ///
    /// Resolved against the board **before** anything is kept, so a config
    /// naming a pin this board does not have is refused with the rig still
    /// running on the one it had. The graphs are *not* uploaded here: loading
    /// says what this rig is and can run, and `Open` is what puts it on the
    /// device — which is what lets somebody load a config to look at it
    /// without disturbing a board mid-experiment.
    fn apply_state_machine_config(&self, config: StateMachineConfig) -> Result<(), tonic::Status> {
        self.with_device(|device| -> Result<(), tonic::Status> {
            if device.is_connected() {
                let resolved = config
                    .line_map
                    .resolved_against(&device.pin_map)
                    .map_err(|problem| {
                        tonic::Status::from(Refusal::from(DeviceProblem::LineMap(problem.to_string())))
                    })?;
                device.line_map = config.line_map.clone();
                device.resolved_line_map = resolved;
                device.push_wiring().map_err(status_for_device)?;
            } else {
                device.line_map = config.line_map.clone();
                device.resolved_line_map = config.line_map.clone();
            }
            Ok(())
        })??;
        let wiring_pushed = self.device_connected();
        self.trace.append(
            KIND_CONFIG_LOADED,
            trace_fields(serde_json::json!({
                "state_machine_config": config.name,
                "board": if config.board.is_empty() { None } else { Some(&config.board) },
                "graph_names": config.graphs.iter().map(|graph| &graph.name).collect::<Vec<_>>(),
                "wiring_pushed": wiring_pushed,
            })),
        );
        *self.loaded_config.lock().map_err(poisoned)? = Some(config);
        Ok(())
    }

    /// Whether the board is armed or running a trial, by its own report.
    ///
    /// With no board there is nothing to be busy, so the answer is no rather
    /// than a refusal.
    fn a_trial_is_armed_or_running(&self) -> Result<bool, tonic::Status> {
        let report = self.read_device_state()?;
        Ok(report.get("running").and_then(serde_json::Value::as_bool) == Some(true)
            || report.get("link_state").and_then(serde_json::Value::as_i64) == Some(2))
    }

    /// The session as a panel shows it: three facts, reported separately
    /// rather than collapsed into one `ready`, because the useful question at
    /// two in the morning is *which* of them is missing. A config may be loaded
    /// with no session open, and a set may be committed on the board from a
    /// session that ended — which is normal, and is what makes a reconnect
    /// cheap.
    fn session_state(&self) -> Result<wire::SessionState, tonic::Status> {
        let stored_config_names = self.configs.stored_names();
        let opened_at = *self.session_opened_at.lock().map_err(poisoned)?;
        let opened_at_unix_seconds = opened_at.map(|at| {
            at.duration_since(std::time::UNIX_EPOCH)
                .map(|since| since.as_secs_f64())
                .unwrap_or(0.0)
        });
        // `still_in_the_store` is a real question: a loaded config can be
        // deleted while it is loaded, and the rig keeps running it.
        let state_machine_config = self.loaded_config.lock().map_err(poisoned)?.as_ref().map(
            |config| wire::LoadedStateMachineConfig {
                name: config.name.clone(),
                description: config.description.clone(),
                board: config.board.clone(),
                graph_names: config.graphs.iter().map(|graph| graph.name.clone()).collect(),
                still_in_the_store: stored_config_names.contains(&config.name),
            },
        );
        let committed_set = self
            .device
            .lock()
            .map_err(poisoned)?
            .committed_graph_set
            .as_ref()
            .map(committed_set_to_wire);
        Ok(wire::SessionState {
            state_machine_config,
            committed_set,
            session_open: opened_at.is_some(),
            opened_at_unix_seconds,
            // Worked out on this side, against this daemon's own clock. A
            // browser subtracting a rig's timestamp from its own goes negative
            // on a box whose NTP has not settled.
            open_seconds: opened_at.map(|at| {
                std::time::SystemTime::now()
                    .duration_since(at)
                    .map(|open| open.as_secs_f64())
                    .unwrap_or(0.0)
            }),
            active_graph: self.active_graph.lock().map_err(poisoned)?.clone().unwrap_or_default(),
            stored_config_names,
        })
    }

    /// Say which graph a trial gets when it does not name one, or that none
    /// does.
    ///
    /// Checked against the loaded config, and against the committed set if
    /// there is one, because the alternative is a selection that looks fine in
    /// the panel and refuses at the moment somebody presses "run a trial".
    fn select_active_graph(&self, graph_name: Option<String>) -> Result<Option<String>, tonic::Status> {
        let Some(graph_name) = graph_name else {
            *self.active_graph.lock().map_err(poisoned)? = None;
            self.trace.append(
                KIND_ACTIVE_GRAPH_SELECTED,
                trace_fields(serde_json::json!({ "graph": null })),
            );
            return Ok(None);
        };
        let config = self.require_state_machine_config()?;
        let known: Vec<&str> = config.graphs.iter().map(|graph| graph.name.as_str()).collect();
        if !known.contains(&graph_name.as_str()) {
            return Err(graph_not_available(format!(
                "the loaded config '{}' has no graph called '{graph_name}'. Has: {}",
                config.name,
                if known.is_empty() { "(none)".to_string() } else { known.join(", ") }
            )));
        }
        let on_the_board: Option<Vec<String>> = self
            .device
            .lock()
            .map_err(poisoned)?
            .committed_graph_set
            .as_ref()
            .map(|committed| committed.graphs_by_slot.iter().map(|graph| graph.name.clone()).collect());
        if let Some(on_the_board) = on_the_board {
            if !on_the_board.contains(&graph_name) {
                return Err(graph_not_available(format!(
                    "'{graph_name}' is in the config but not in the set the board is holding \
                     ({}). Open a session to put it there.",
                    on_the_board.join(", ")
                )));
            }
        }
        *self.active_graph.lock().map_err(poisoned)? = Some(graph_name.clone());
        self.trace.append(
            KIND_ACTIVE_GRAPH_SELECTED,
            trace_fields(serde_json::json!({ "graph": graph_name })),
        );
        Ok(Some(graph_name))
    }

    /// Greet the board, and write down what answered.
    ///
    /// The firmware is recorded, not refused: a board running another build
    /// still runs, and whether that is acceptable is the operator's call. What
    /// must not happen is nobody being able to find out afterwards.
    pub fn connect(&self) -> Result<serde_json::Value, tonic::Status> {
        let (hello_ack, connection_count) = self
            .with_device(|device| {
                device
                    .connect_and_greet()
                    .map(|hello_ack| (hello_ack, device.connection_count))
            })?
            .map_err(status_for_device)?;
        let running = hello_ack.get("fw").and_then(serde_json::Value::as_str);
        let installed = installed_firmware_version(std::path::Path::new(INSTALLED_MANIFEST));
        let firmware = compare_firmware(running, installed.as_deref());
        self.trace.append(
            KIND_LINK_CONNECTED,
            trace_fields(serde_json::json!({
                "target": self.configuration.device_target,
                "board": hello_ack.get("board"),
                "firmware_version": hello_ack.get("fw"),
                "installed_firmware_version": firmware.installed,
                "firmware_matches_package":
                    if firmware.comparable { Some(firmware.matches) } else { None },
                "connection_count": connection_count,
            })),
        );
        Ok(hello_ack)
    }

    /// Load the configured config, and connect if told to.
    ///
    /// The config first, so the line map exists before the greeting: the
    /// wiring is pushed as part of connecting, and a board greeted with no map
    /// is a board holding every line at a default `safe` nobody chose.
    ///
    /// **Neither is fatal.** A daemon that refused to start because a config
    /// was deleted, or a board was unplugged, would take the API down with it
    /// — and the API is how somebody finds out.
    pub fn start(&self) {
        let config_name = self.configuration.startup_state_machine_config.clone();
        if !config_name.is_empty() {
            let loaded = self
                .configs
                .load(&config_name)
                .map_err(config_problem)
                .and_then(|config| self.apply_state_machine_config(config));
            if let Err(problem) = loaded {
                self.note_an_error(format!(
                    "the startup state-machine config '{config_name}' did not load: {}",
                    refusal::detail_of(&problem)
                ));
            }
        }
        if self.configuration.connect_on_startup {
            if let Err(problem) = self.connect() {
                self.note_an_error(refusal::detail_of(&problem));
            }
        }
    }

    fn note_an_error(&self, sentence: String) {
        log::warn!("{sentence}");
        if let Ok(mut last) = self.last_error_from_the_device.lock() {
            *last = Some(sentence);
        }
    }

    /// One `state_report`, or nothing if there is no board to ask.
    fn read_device_state(&self) -> Result<serde_json::Value, tonic::Status> {
        self.with_device(|device| {
            if !device.is_connected() {
                return Ok(serde_json::Value::Object(Default::default()));
            }
            device.read_state_report()
        })?
        .map_err(status_for_device)
    }

    fn device_state(&self) -> Result<wire::DeviceState, tonic::Status> {
        let state_report = self.read_device_state()?;
        let last_error = self
            .last_error_from_the_device
            .lock()
            .map_err(poisoned)?
            .clone()
            .unwrap_or_default();
        let device = self.device.lock().map_err(poisoned)?;
        Ok(device_state_to_wire(
            &device,
            &self.configuration.device_target,
            &state_report,
            last_error,
        ))
    }

    /// Say the session is over, and write down which trial closing took away.
    fn close_session(
        &self,
        cancelled_trial_id: Option<i64>,
    ) -> Result<tonic::Response<wire::CloseSessionResult>, tonic::Status> {
        let was_open = self.session_opened_at.lock().map_err(poisoned)?.take().is_some();
        let loaded = self.loaded_config.lock().map_err(poisoned)?.as_ref().map(|c| c.name.clone());
        self.trace.append(
            KIND_SESSION_CLOSED,
            trace_fields(serde_json::json!({
                "state_machine_config": loaded,
                "cancelled_trial_id": cancelled_trial_id,
                "was_open": was_open,
            })),
        );
        Ok(tonic::Response::new(wire::CloseSessionResult {
            was_open,
            cancelled_trial_id,
            session: Some(self.session_state()?),
        }))
    }

    /// Every rule, plus **this board's** capacities.
    ///
    /// The capacity half is the half worth having — "you have room for two
    /// more graphs" is what somebody setting up a session wants to know —
    /// which is why this needs a board. Compiled against the *resolved* map,
    /// the one an upload would use.
    fn validate(&self, graph: &GraphDefinition) -> Result<wire::GraphValidation, tonic::Status> {
        let warnings: Vec<wire::GraphWarning> = graph
            .warnings()
            .into_iter()
            .map(|warning| wire::GraphWarning {
                kind: warning.kind,
                state: warning.state,
                transition: warning.transition as i32,
                lines: warning.lines,
                detail: warning.detail,
            })
            .collect();
        let device = self.device.lock().map_err(poisoned)?;
        let Some(capabilities) = &device.capabilities else {
            return Err(Refusal::no_board_attached(
                "a graph is checked against a board's caps, and there is none",
            )
            .into());
        };
        Ok(
            match compile_graph_set_for_device(
                std::slice::from_ref(graph),
                &device.resolved_line_map,
                capabilities,
                0,
            ) {
                // Legal, uploads, runs — and the warnings survive a valid
                // answer: probably narrower than its author thinks.
                Ok(compiled) => wire::GraphValidation {
                    valid: true,
                    detail: String::new(),
                    pool_usage: Some(pool_counts(&compiled.pool_usage)),
                    pool_capacity: Some(pool_counts(&compiled.pool_capacity)),
                    warnings,
                },
                // A refusal has no pools to report, and a zeroed count would
                // read as a board with no room at all.
                Err(problem) => wire::GraphValidation {
                    valid: false,
                    detail: problem.to_string(),
                    pool_usage: None,
                    pool_capacity: None,
                    warnings,
                },
            },
        )
    }

    /// No board, said before anything is loaded or compiled.
    fn require_a_board(&self) -> Result<(), tonic::Status> {
        if self.device_connected() {
            Ok(())
        } else {
            Err(Refusal::no_board_attached("no board is connected").into())
        }
    }
}

/// A selection the loaded config, or the board, cannot honour. Caught at
/// selection rather than at the moment somebody presses run, which is the
/// difference between a refusal and a rig that looks armed.
fn graph_not_available(detail: String) -> tonic::Status {
    Refusal::new(Category::WrongMoment, "graph_not_available", detail, "graph").into()
}

fn graph_names_of(compiled: &CompiledGraphSet) -> Vec<&str> {
    compiled.graphs_by_slot.iter().map(|graph| graph.name.as_str()).collect()
}

/// What this board can hold, or nothing when none has said: a board with
/// `max_states = 0` and a board that has not greeted are different situations.
fn capacities_to_wire(capabilities: &DeviceCapabilities) -> wire::DeviceCapacities {
    wire::DeviceCapacities {
        max_line: capabilities.max_line as i32,
        max_states: capabilities.max_states as i32,
        max_transitions: capabilities.max_transitions as i32,
        max_output_actions: capabilities.max_output_actions as i32,
        max_distributions: capabilities.max_distributions as i32,
        max_choice_options: capabilities.max_choice_options as i32,
        max_path: capabilities.max_path as i32,
        max_graphs: capabilities.max_graphs as i32,
        max_timers: capabilities.max_timers as i32,
        first_timer_line: capabilities.first_timer_line as i32,
        input_line_count: capabilities.input_line_count as i32,
        output_line_count: capabilities.output_line_count as i32,
    }
}

/// The rig's lines, as somebody looking at the wiring sees them.
///
/// **The resolved map, not the configured one**, so a line written as a pin
/// reports the index it will actually be uploaded with. Before a board has been
/// attached the two are the same and the index is absent, which is a different
/// thing from being zero.
///
/// `is_high_now` is absent rather than false when there is no word to read it
/// from: there is no read-back path from a pin, so the words are the device's
/// own account — and "low" and "nobody asked the board" are the difference
/// between a wiring fault and a disconnected cable.
fn line_map_view(
    device: &StatemachinedDevice,
    input_word: Option<i64>,
    output_word: Option<i64>,
) -> wire::LineMapView {
    // The resolved map where there is a board to resolve against.
    let map = if device.is_connected() { &device.resolved_line_map } else { &device.line_map };
    let pin_map = &device.pin_map;
    let high = |word: Option<i64>, index: Option<i64>| match (word, index) {
        (Some(word), Some(index)) => Some((word >> index) & 1 == 1),
        _ => None,
    };
    wire::LineMapView {
        input_lines: map
            .input_lines
            .iter()
            .map(|line| wire::InputLine {
                name: line.name.clone(),
                line_index: line.line_index.map(|index| index as i32),
                // As the config wrote it. The board's own names for its pins
                // travel beside the lines, in `board_input_pins`.
                pin_label: line.pin_label.clone(),
                reads_active_low: line.reads_active_low,
                is_enabled: line.is_enabled,
                debounce_milliseconds: line.debounce_milliseconds as i32,
                is_high_now: high(input_word, line.line_index),
            })
            .collect(),
        output_lines: map
            .output_lines
            .iter()
            .map(|line| wire::OutputLine {
                name: line.name.clone(),
                line_index: line.line_index.map(|index| index as i32),
                pin_label: line.pin_label.clone(),
                safe_level_is_high: line.safe_level_is_high,
                is_high_now: high(output_word, line.line_index),
            })
            .collect(),
        pin_labels_came_from: source_of(pin_map.source).to_string(),
        board_input_pins: pin_map.input_pin_labels.clone(),
        board_output_pins: pin_map.output_pin_labels.clone(),
    }
}

/// Where a pin label came from, as the wire spells it.
fn source_of(source: PinLabelSource) -> &'static str {
    match source {
        PinLabelSource::Device => "device",
        PinLabelSource::Assumed => "assumed",
        PinLabelSource::Unknown => "unknown",
    }
}

/// A device failure, as the status a client sees.
fn status_for_device(problem: DeviceProblem) -> tonic::Status {
    Refusal::from(problem).into()
}

fn config_summaries(state: &DaemonState) -> wire::StateMachineConfigSummaries {
    wire::StateMachineConfigSummaries {
        configs: state
            .configs
            .stored_names()
            .iter()
            .map(|name| config_summary(&state.configs, name))
            .collect(),
        loaded: state.loaded_config_name(),
    }
}

// -- State ---------------------------------------------------------------------
#[tonic::async_trait]
impl service::state_server::State for DaemonServices {
    type WatchStateStream = std::pin::Pin<Box<dyn tokio_stream::Stream<Item = Result<crate::wire::statemachined::v1::StateFrame, tonic::Status>> + Send>>;

    type WatchTraceStream = std::pin::Pin<Box<dyn tokio_stream::Stream<Item = Result<crate::wire::statemachined::v1::TraceEntry, tonic::Status>> + Send>>;

    async fn read_state(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadStateRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::RigState>, tonic::Status> {
        let state = self.state.clone();
        off_the_runtime(move || Ok(tonic::Response::new(state.rig_state()?))).await
    }

    async fn watch_state(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::WatchStateRequest>,
    ) -> Result<tonic::Response<Self::WatchStateStream>, tonic::Status> {
        // The current state at once, then one frame per change. **At once**: a
        // client that connected to a quiet rig and saw nothing could not tell
        // that from a rig that is not there. Frames are coalesced by
        // construction — each is a whole state read when it is sent — so
        // `sequence` counts what was sent and nobody reconciles a backlog.
        let watcher = Watching::register(&self.state, &request, "state");
        let (sender, receiver) = tokio::sync::mpsc::channel(4);
        tokio::spawn(async move {
            let mut sequence = 0i64;
            let mut previous: Option<wire::RigState> = None;
            loop {
                let reading = {
                    let state = watcher.state.clone();
                    tokio::task::spawn_blocking(move || state.rig_state()).await
                };
                let reading = match reading {
                    Ok(Ok(reading)) => reading,
                    Ok(Err(status)) => {
                        let _ = sender.send(Err(status)).await;
                        return;
                    }
                    Err(_) => return,
                };
                if previous.as_ref() != Some(&reading) {
                    let frame = wire::StateFrame {
                        sequence,
                        frame: Some(wire::state_frame::Frame::State(reading.clone())),
                    };
                    if sender.send(Ok(frame)).await.is_err() {
                        return;
                    }
                    sequence += 1;
                    previous = Some(reading);
                    watcher.state.observers.note_delivery(watcher.observer_id, 1);
                }
                tokio::select! {
                    _ = sender.closed() => return,
                    _ = tokio::time::sleep(WATCH_PERIOD) => {}
                }
            }
        });
        Ok(tonic::Response::new(Box::pin(
            tokio_stream::wrappers::ReceiverStream::new(receiver),
        )))
    }

    async fn read_trace(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::ReadTraceRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::TraceWindow>, tonic::Status> {
        let request = request.into_inner();
        let trace = &self.state.trace;
        let oldest = trace.oldest_entry_number_still_held();
        let limit = if request.limit > 0 { request.limit as usize } else { 500 };
        Ok(tonic::Response::new(trace::trace_window_to_wire(
            &trace.entries_since(request.since_entry_number, limit),
            trace.newest_entry_number(),
            oldest,
            trace.ring_capacity(),
            trace
                .has_fallen_out_of_the_ring(request.since_entry_number)
                .then_some(oldest),
        )))
    }

    async fn watch_trace(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::WatchTraceRequest>,
    ) -> Result<tonic::Response<Self::WatchTraceStream>, tonic::Status> {
        // Everything from `since_entry_number` onwards, and then as it
        // happens. **This is what triald subscribes to.** The backlog goes
        // first so a subscriber that reconnects picks up where it left off
        // rather than losing whatever happened while it was away.
        let watcher = Watching::register(&self.state, &request, "trace");
        let mut next_entry_number = request.into_inner().since_entry_number;
        let (sender, receiver) = tokio::sync::mpsc::channel(64);
        tokio::spawn(async move {
            let state = watcher.state.clone();
            let trace = &state.trace;
            loop {
                if trace.has_fallen_out_of_the_ring(next_entry_number) {
                    // Behind the ring, and entries are gone. Noted rather than
                    // refused: the stream carries on from whatever is left, and
                    // the client sees the gap in the entry numbers.
                    state.observers.note_fell_behind(watcher.observer_id);
                    next_entry_number = trace.oldest_entry_number_still_held();
                }
                let entries = trace.entries_since(next_entry_number, 500);
                for entry in &entries {
                    if sender.send(Ok(trace::trace_entry_to_wire(entry))).await.is_err() {
                        return; // the client went away; `watcher` unregisters
                    }
                    next_entry_number =
                        entry.get("entry_number").and_then(serde_json::Value::as_i64).unwrap_or(0) + 1;
                }
                state.observers.note_delivery(watcher.observer_id, entries.len() as i64);
                if entries.is_empty() {
                    tokio::select! {
                        _ = sender.closed() => return,
                        _ = tokio::time::sleep(WATCH_PERIOD) => {}
                    }
                }
            }
        });
        Ok(tonic::Response::new(Box::pin(
            tokio_stream::wrappers::ReceiverStream::new(receiver),
        )))
    }

    async fn read_trial_trace(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::ReadTrialTraceRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::TrialTrace>, tonic::Status> {
        let trial_id = request.into_inner().trial_id;
        Ok(tonic::Response::new(wire::TrialTrace {
            trial_id,
            entries: self
                .state
                .trace
                .entries_for_trial(trial_id)
                .iter()
                .map(trace::trace_entry_to_wire)
                .collect(),
        }))
    }

    async fn read_observers(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadObserversRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::Observers>, tonic::Status> {
        Ok(tonic::Response::new(trace::observers_to_wire(
            &self.state.observers.observers(),
        )))
    }

}

// -- Trial ---------------------------------------------------------------------
#[tonic::async_trait]
impl service::trial_server::Trial for DaemonServices {
    async fn configure(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::ConfigureTrialRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::ConfigureTrialResult>, tonic::Status> {
        let state = self.state.clone();
        let request = request.into_inner();
        off_the_runtime(move || {
            state.require_a_board()?;
            Ok(tonic::Response::new(state.configure_trial(&request)?))
        })
        .await
    }

    async fn start(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::StartTrialRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::StartTrialResult>, tonic::Status> {
        let state = self.state.clone();
        let trial_id = request.into_inner().trial_id;
        off_the_runtime(move || {
            state.require_a_board()?;
            let started = state.start_trial(trial_id)?;
            Ok(tonic::Response::new(wire::StartTrialResult {
                trial_id,
                started_device_microseconds: started
                    .get("at_us")
                    .and_then(serde_json::Value::as_i64)
                    .unwrap_or(0),
            }))
        })
        .await
    }

    async fn cancel(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::CancelTrialRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::CancelTrialResult>, tonic::Status> {
        // **A cancel that races a terminal state comes back with the real
        // outcome**, passed through rather than rewritten to CANCELLED.
        let state = self.state.clone();
        let trial_id = request.into_inner().trial_id;
        off_the_runtime(move || {
            state.require_a_board()?;
            let acknowledgement = state.cancel_trial(trial_id)?;
            Ok(tonic::Response::new(wire::CancelTrialResult {
                trial_id,
                cancelled: acknowledgement
                    .get("cancelled")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false),
                outcome_code: acknowledgement
                    .get("outcome")
                    .and_then(serde_json::Value::as_i64)
                    .unwrap_or(0) as i32,
            }))
        })
        .await
    }

    async fn read_result(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::ReadTrialResultRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::TrialResult>, tonic::Status> {
        // **Only the last one.** The trace is the history; naming an older
        // trial is refused rather than answered with the wrong trial, which is
        // the mistake the whole addressing scheme exists to prevent.
        let asked = request.into_inner().trial_id;
        let last = self.state.last_trial_result.lock().map_err(poisoned)?.clone();
        let Some(result) = last else {
            return Err(Refusal::new(
                Category::WrongMoment,
                "no_result_yet",
                "no trial has completed on this connection",
                "trial",
            )
            .into());
        };
        if let Some(asked) = asked.filter(|asked| *asked != result.trial_id) {
            return Err(Refusal::new(
                Category::NoSuchThing,
                "not_the_last_trial",
                format!(
                    "trial {asked} is not the last one to complete; that was trial {}",
                    result.trial_id
                ),
                "trial_id",
            )
            .into());
        }
        Ok(tonic::Response::new(trial::trial_result_to_wire(&result)))
    }

}

// -- Device --------------------------------------------------------------------
#[tonic::async_trait]
impl service::device_server::Device for DaemonServices {
    type WatchSerialMonitorStream = std::pin::Pin<Box<dyn tokio_stream::Stream<Item = Result<crate::wire::statemachined::v1::SerialMonitorEntry, tonic::Status>> + Send>>;

    async fn read_device(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadDeviceRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::DeviceState>, tonic::Status> {
        let state = self.state.clone();
        off_the_runtime(move || Ok(tonic::Response::new(state.device_state()?))).await
    }

    async fn open_link(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::OpenLinkRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::DeviceState>, tonic::Status> {
        // Greet, which is what **takes the rig** from a board that was arming
        // its own trials. Opening the port does not do that; the greeting does,
        // and it is the whole point of the handover that a serial monitor
        // cannot trigger it.
        let state = self.state.clone();
        off_the_runtime(move || {
            state.connect()?;
            Ok(tonic::Response::new(state.device_state()?))
        })
        .await
    }

    async fn read_lines(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadLinesRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::LineMapView>, tonic::Status> {
        let state = self.state.clone();
        off_the_runtime(move || {
            let report = state.read_device_state()?;
            let word = |key: &str| {
                report.get("io").and_then(|io| io.get(key)).and_then(serde_json::Value::as_i64)
            };
            let device = state.device.lock().map_err(poisoned)?;
            Ok(tonic::Response::new(line_map_view(&device, word("in"), word("out"))))
        })
        .await
    }

    async fn write_line_map_file(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::StoredFile>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::WriteLineMapResult>, tonic::Status> {
        unported!("Device/write_line_map_file")
    }

    async fn read_serial_monitor(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadSerialMonitorRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::SerialMonitorWindow>, tonic::Status> {
        unported!("Device/read_serial_monitor")
    }

    async fn watch_serial_monitor(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::WatchSerialMonitorRequest>,
    ) -> Result<tonic::Response<Self::WatchSerialMonitorStream>, tonic::Status> {
        unported!("Device/watch_serial_monitor")
    }

    async fn read_firmware(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadFirmwareRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::FirmwareVersions>, tonic::Status> {
        // **The comparison is what matters**: a board in a rack cannot be
        // asked which commit it is running. Flashing is deliberately not here.
        let running = self
            .state
            .device
            .lock()
            .map_err(poisoned)?
            .hello_ack
            .as_ref()
            .and_then(|ack| ack.get("fw"))
            .map(|fw| fw.as_str().map(str::to_string).unwrap_or_else(|| fw.to_string()));
        let Some(running) = running else {
            return Err(Refusal::no_board_attached(
                "no board is connected, so nothing is running",
            )
            .into());
        };
        let installed = installed_firmware_version(std::path::Path::new(INSTALLED_MANIFEST));
        let comparison = compare_firmware(Some(&running), installed.as_deref());
        Ok(tonic::Response::new(wire::FirmwareVersions {
            running: comparison.running.unwrap_or_default(),
            installed: comparison.installed.unwrap_or_default(),
            running_is_stamped: comparison.running_is_stamped,
            comparable: comparison.comparable,
            matches: comparison.matches,
        }))
    }

    async fn read_autorun(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadAutorunRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::Autorun>, tonic::Status> {
        unported!("Device/read_autorun")
    }

    async fn write_autorun(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::WriteAutorunRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::Autorun>, tonic::Status> {
        unported!("Device/write_autorun")
    }

    async fn save_settings(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::SaveSettingsRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::SaveSettingsResult>, tonic::Status> {
        unported!("Device/save_settings")
    }

}

// -- GraphStore ----------------------------------------------------------------
#[tonic::async_trait]
impl service::graph_store_server::GraphStore for DaemonServices {
    async fn list_graphs(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ListGraphsRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::GraphSummaries>, tonic::Status> {
        Ok(tonic::Response::new(graph_summaries(&self.state.graphs)))
    }

    async fn read_graph_file(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::ReadFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::StoredFile>, tonic::Status> {
        // **Re-serialised, not the bytes on disk.** What an editor gets is
        // the graph as this daemon understands it, which is what makes the
        // download/upload round trip a property of one parser rather than of
        // whatever formatting the file happened to have.
        let name = request.into_inner().name;
        let graph = self.state.graphs.load(&name).map_err(graph_problem)?;
        Ok(tonic::Response::new(wire::StoredFile {
            name: graph.name.clone(),
            text: graph.to_json(),
        }))
    }

    async fn write_graph_file(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::StoredFile>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::GraphSummary>, tonic::Status> {
        // What crosses from an editor is the file's *text*, and it goes through
        // this daemon's own parser. The name inside the document wins over the
        // one beside it: a graph is stored under what it calls itself.
        let file = request.into_inner();
        let graph = self
            .state
            .graphs
            .write_text(&file.text, &file.name)
            .map_err(graph_problem)?;
        Ok(tonic::Response::new(graph_summary(&self.state.graphs, &graph.name)))
    }

    async fn delete_graph(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::DeleteFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::GraphSummaries>, tonic::Status> {
        // **Refused while the board holds it.** Deleting the file does not
        // stop a trial; what it does is make the paradigm that ran
        // unreproducible, halfway through the session that ran it.
        let name = request.into_inner().name;
        let in_use = self
            .state
            .device
            .lock()
            .map_err(poisoned)?
            .committed_graph_set
            .as_ref()
            .is_some_and(|committed| committed.graphs_by_slot.iter().any(|graph| graph.name == name));
        if in_use {
            return Err(Refusal::new(
                Category::WrongMoment,
                "graph_in_use",
                format!("'{name}' is in the committed set and a trial could still name it"),
                "name",
            )
            .into());
        }
        self.state.graphs.delete(&name).map_err(graph_problem)?;
        Ok(tonic::Response::new(graph_summaries(&self.state.graphs)))
    }

    async fn validate_graph(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::ReadFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::GraphValidation>, tonic::Status> {
        let graph = self.state.graphs.load(&request.into_inner().name).map_err(graph_problem)?;
        Ok(tonic::Response::new(self.state.validate(&graph)?))
    }

    async fn validate_graph_file(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::FileDraft>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::GraphValidation>, tonic::Status> {
        // The same check against a **draft**: what somebody is typing. Through
        // this daemon's own parser and compiler, so what comes back is the
        // refusal the real thing would give.
        let graph = GraphDefinition::from_json(&request.into_inner().text).map_err(|problem| {
            tonic::Status::from(Refusal::new(Category::BadRequest, "bad_graph", problem.0, "text"))
        })?;
        Ok(tonic::Response::new(self.state.validate(&graph)?))
    }

    async fn upload_graph(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::ReadFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::CommittedGraphSet>, tonic::Status> {
        // One graph on the board on its own — a bench convenience. **Refused
        // while a session's set is committed**, because the board holds one
        // set and this replaces it: losing a session's paradigms because
        // somebody previewed a graph is not a recoverable mistake. A set of one
        // is not a session, so that case is allowed through.
        let state = self.state.clone();
        let name = request.into_inner().name;
        off_the_runtime(move || {
            state.require_a_board()?;
            let committed_graphs = state
                .device
                .lock()
                .map_err(poisoned)?
                .committed_graph_set
                .as_ref()
                .map(|committed| committed.graphs_by_slot.len());
            if let Some(count) = committed_graphs.filter(|count| *count > 1) {
                return Err(Refusal::new(
                    Category::WrongMoment,
                    "session_set_committed",
                    format!(
                        "a session's set of {count} graphs is committed; uploading one graph \
                         would replace it"
                    ),
                    "name",
                )
                .into());
            }
            let (compiled, _elapsed) = state.upload_session_graph_set(&[name])?;
            Ok(tonic::Response::new(committed_set_to_wire(&compiled)))
        })
        .await
    }

}

// -- StateMachineConfigStore ---------------------------------------------------
#[tonic::async_trait]
impl service::state_machine_config_store_server::StateMachineConfigStore for DaemonServices {
    async fn list_configs(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ListConfigsRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::StateMachineConfigSummaries>, tonic::Status> {
        Ok(tonic::Response::new(config_summaries(&self.state)))
    }

    async fn read_config_file(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::ReadFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::StoredFile>, tonic::Status> {
        let name = request.into_inner().name;
        let config = self.state.configs.load(&name).map_err(config_problem)?;
        Ok(tonic::Response::new(wire::StoredFile {
            name: config.name.clone(),
            text: config.to_json(),
        }))
    }

    async fn write_config_file(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::StoredFile>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::StateMachineConfigSummaries>, tonic::Status> {
        // Answers with the **whole store**, as DeleteConfig does, rather than
        // with the one summary: `loaded` beside the listing is what says
        // whether this write landed on the config the rig is running, which is
        // the question somebody editing during a session is actually asking.
        let file = request.into_inner();
        self.state
            .configs
            .write_text(&file.text, &file.name)
            .map_err(config_problem)?;
        Ok(tonic::Response::new(config_summaries(&self.state)))
    }

    async fn delete_config(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::DeleteFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::StateMachineConfigSummaries>, tonic::Status> {
        let name = request.into_inner().name;
        self.state.configs.delete(&name).map_err(config_problem)?;
        Ok(tonic::Response::new(config_summaries(&self.state)))
    }

    async fn load_config(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::ReadFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::LoadedConfigResult>, tonic::Status> {
        // Apply its line map and push the wiring; the graphs go up at `Open`.
        // Refused while a trial is armed or running, like every other change
        // to the wiring: a line map is what a trial's record *means*, and
        // moving it mid-trial makes that record a fiction.
        let state = self.state.clone();
        let name = request.into_inner().name;
        off_the_runtime(move || {
            if state.a_trial_is_armed_or_running()? {
                return Err(Refusal::new(
                    Category::WrongMoment,
                    "busy",
                    "a trial is armed or running",
                    "config_name",
                )
                .into());
            }
            let config = state.configs.load(&name).map_err(config_problem)?;
            let graph_names = config.graphs.iter().map(|graph| graph.name.clone()).collect();
            let loaded = config.name.clone();
            state.apply_state_machine_config(config)?;
            let device = state.device.lock().map_err(poisoned)?;
            // `wiring_pushed` is a real distinction: a config loads with nothing
            // attached, and reaches the board the moment one greets. A caller
            // that assumed the wiring was live would be assuming a lamp it
            // cannot see.
            Ok(tonic::Response::new(wire::LoadedConfigResult {
                loaded,
                wiring_pushed: device.is_connected(),
                line_map: Some(line_map_view(&device, None, None)),
                graph_names,
            }))
        })
        .await
    }

}

// -- Session -------------------------------------------------------------------
#[tonic::async_trait]
impl service::session_server::Session for DaemonServices {
    async fn read_session(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadSessionRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::SessionState>, tonic::Status> {
        Ok(tonic::Response::new(self.state.session_state()?))
    }

    async fn open(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::OpenSessionRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::OpenSessionResult>, tonic::Status> {
        // Put the loaded config's graphs on the device. What triald does at the
        // top of a session and what the panel's button does on a bench — the
        // same call, because a bench that exercised a different path would be
        // a bench that proves nothing about the rig.
        let state = self.state.clone();
        off_the_runtime(move || {
            state.require_a_board()?;
            let (config, compiled, elapsed) = state.open_session()?;
            Ok(tonic::Response::new(open_session_result(&compiled, config.name, elapsed)))
        })
        .await
    }

    async fn upload_graphs(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::UploadGraphsRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::OpenSessionResult>, tonic::Status> {
        // The same upload `Open` does, composed by hand rather than from a
        // config. **The slowest call in this API by a wide margin, and the one
        // where a session is allowed to fail**: a graph that does not fit this
        // board fails here, with an animal not yet in the booth.
        let state = self.state.clone();
        let graph_names = request.into_inner().graph_names;
        off_the_runtime(move || {
            state.require_a_board()?;
            let (compiled, elapsed) = state.upload_session_graph_set(&graph_names)?;
            Ok(tonic::Response::new(open_session_result(
                &compiled,
                state.loaded_config_name(),
                elapsed,
            )))
        })
        .await
    }

    async fn close(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::CloseSessionRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::CloseSessionResult>, tonic::Status> {
        // Say the session is over, and leave the device holding its set.
        // **What this does not do is unload the board**: the committed set
        // surviving is what makes a reconnect cheap and lets a session resume
        // after a daemon restart.
        //
        // The one safety act: a trial still armed is cancelled, because an
        // armed trial with nobody driving it will run at whatever time
        // somebody next touches a lever. Reported, never fatal.
        let state = self.state.clone();
        off_the_runtime(move || {
            let (cancelled_trial_id, connected) = {
                let device = state.device.lock().map_err(poisoned)?;
                (device.armed_trial_id, device.is_connected())
            };
            if let (Some(trial_id), true) = (cancelled_trial_id, connected) {
                if let Err(problem) = state.try_to_cancel(trial_id)? {
                    *state.last_error_from_the_device.lock().map_err(poisoned)? =
                        Some(problem.to_string());
                }
            }
            state.close_session(cancelled_trial_id)
        })
        .await
    }

    async fn set_active_graph(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::SetActiveGraphRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::ActiveGraph>, tonic::Status> {
        let selected = self.state.select_active_graph(Some(request.into_inner().graph))?;
        Ok(tonic::Response::new(wire::ActiveGraph {
            active_graph: selected.unwrap_or_default(),
        }))
    }

    async fn clear_active_graph(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ClearActiveGraphRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::ActiveGraph>, tonic::Status> {
        self.state.select_active_graph(None)?;
        Ok(tonic::Response::new(wire::ActiveGraph::default()))
    }

}

// -- Recording -----------------------------------------------------------------
#[tonic::async_trait]
impl service::recording_server::Recording for DaemonServices {
    async fn read_recordings(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadRecordingsRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::Recordings>, tonic::Status> {
        unported!("Recording/read_recordings")
    }

    async fn start(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::StartRecordingRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::RecordingManifest>, tonic::Status> {
        unported!("Recording/start")
    }

    async fn pause(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::PauseRecordingRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::RecordingManifest>, tonic::Status> {
        unported!("Recording/pause")
    }

    async fn resume(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ResumeRecordingRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::RecordingManifest>, tonic::Status> {
        unported!("Recording/resume")
    }

    async fn stop(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::StopRecordingRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::RecordingManifest>, tonic::Status> {
        unported!("Recording/stop")
    }

    async fn clear(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ClearRecordingRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::RecordingManifest>, tonic::Status> {
        unported!("Recording/clear")
    }

    async fn read_recording(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::RecordingName>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::RecordingManifest>, tonic::Status> {
        unported!("Recording/read_recording")
    }

    async fn read_entries(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadRecordingEntriesRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::RecordingEntries>, tonic::Status> {
        unported!("Recording/read_entries")
    }

    async fn delete_recording(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::RecordingName>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::Recordings>, tonic::Status> {
        unported!("Recording/delete_recording")
    }

}

// -- Configuration -------------------------------------------------------------
#[tonic::async_trait]
impl service::configuration_server::Configuration for DaemonServices {
    async fn read_configuration(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadConfigurationRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::RigConfiguration>, tonic::Status> {
        Ok(tonic::Response::new(rig_configuration_to_wire(
            &self.state.configuration,
        )))
    }

    async fn patch_configuration(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::RigConfigurationPatch>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::RigConfigurationUpdate>, tonic::Status> {
        unported!("Configuration/patch_configuration")
    }

    /// Is the daemon up, and does it have a board.
    ///
    /// **Two different questions, and a supervisor only asks the first**: a
    /// daemon whose board is unplugged is still the thing you ask *why*. So
    /// this answers rather than refusing when there is no device, and
    /// `device_connected` is a field instead of a status code.
    ///
    /// Answered without touching the board, so it stays cheap enough to poll
    /// and cannot be made to hang by a serial port that is not draining.
    async fn read_health(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadHealthRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::Health>, tonic::Status> {
        Ok(tonic::Response::new(crate::wire::statemachined::v1::Health {
            ok: true,
            device_connected: self.state.device_connected(),
        }))
    }

}
