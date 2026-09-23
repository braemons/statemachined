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
use crate::device::device_pin_map::{Direction, PinLabelSource};
use crate::device::statemachined_device::{DeviceProblem, StatemachinedDevice};
use crate::rig_configuration::RigConfiguration;
use crate::device::statemachined_device::UploadProblem;
use crate::graph_set_compiler::CompiledGraphSet;
use crate::model::graph_definition::GraphDefinition;
use crate::model::state_machine_config::StateMachineConfig;
use crate::store::{Document, Store, StoreProblem};
use crate::wire::statemachined::v1 as wire;

use crate::wire::statemachined::v1::service;

mod refusal;
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

/// The board, as the wire describes it.
///
/// **Answered from what the daemon already knows**, never by asking the board:
/// a status call that opened a link or waited on a serial port would be a
/// status call that can hang, and this is the one a supervisor polls.
fn device_state_to_wire(device: &StatemachinedDevice) -> wire::DeviceState {
    let ack = device.hello_ack.clone().unwrap_or(serde_json::Value::Null);
    let text = |key: &str| {
        ack.get(key)
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default()
            .to_string()
    };
    let number = |key: &str| ack.get(key).and_then(serde_json::Value::as_i64).unwrap_or(0);
    wire::DeviceState {
        connected: device.is_connected(),
        target: device.target.clone(),
        board: text("board"),
        firmware_version: text("fw"),
        protocol_version: number("proto") as i32,
        measured_scan_hz: number("scan_hz") as i32,
        capacities: capacities_of(&ack),
        has_wiring: ack.get("has_wiring").and_then(serde_json::Value::as_bool),
        // The whole reason the `pins` command exists: before it there was no
        // third answer to "which pin is line 4" beyond a table copied out of
        // the firmware. So the provenance travels with the answer.
        pin_labels_came_from: source_of(device.pin_map.source).to_string(),
        uptime_device_microseconds: number("t_us"),
        committed_set: device.committed_graph_set.as_ref().map(committed_set_to_wire),
        ..Default::default()
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
        self.upload_graph_set(&graphs)
    }

    /// Compile, check against this board's caps, upload, commit, and say a
    /// session is open. Returns what it cost in milliseconds — the slowest
    /// call in the API, and the one a panel shows a progress bar for.
    fn upload_graph_set(
        &self,
        graphs: &[GraphDefinition],
    ) -> Result<(CompiledGraphSet, u128), tonic::Status> {
        // Timed from before the lock, as Python times it: a caller waiting on
        // somebody else's upload is waiting on this call.
        let started = std::time::Instant::now();
        let compiled = {
            let mut device = self.device.lock().map_err(poisoned)?;
            let set_version = Self::next_set_version(&device);
            device
                .upload_graph_set(graphs, set_version)
                .map_err(|problem| tonic::Status::from(Refusal::from(problem)))?
        };
        let elapsed_milliseconds = started.elapsed().as_millis();
        *self.session_opened_at.lock().map_err(poisoned)? = Some(std::time::SystemTime::now());
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
        {
            let mut device = self.device.lock().map_err(poisoned)?;
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
        }
        *self.loaded_config.lock().map_err(poisoned)? = Some(config);
        Ok(())
    }

    /// Whether the board is armed or running a trial, by its own report.
    ///
    /// With no board there is nothing to be busy, so the answer is no rather
    /// than a refusal.
    fn a_trial_is_armed_or_running(&self) -> Result<bool, tonic::Status> {
        let mut device = self.device.lock().map_err(poisoned)?;
        if !device.is_connected() {
            return Ok(false);
        }
        let report = device.read_state_report().map_err(status_for_device)?;
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
        Ok(Some(graph_name))
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

fn capacities_of(ack: &serde_json::Value) -> Option<wire::DeviceCapacities> {
    let caps = ack.get("caps")?;
    let number = |key: &str| caps.get(key).and_then(serde_json::Value::as_i64).unwrap_or(0) as i32;
    Some(wire::DeviceCapacities {
        max_line: number("max_line"),
        max_states: number("max_states"),
        max_transitions: number("max_transitions"),
        max_output_actions: number("max_output_actions"),
        max_distributions: number("max_distributions"),
        max_choice_options: number("max_choice_options"),
        max_path: number("max_path"),
        max_graphs: number("max_graphs"),
        max_timers: number("max_timers"),
        ..Default::default()
    })
}

/// The rig's lines, as somebody looking at the wiring sees them.
///
/// **The resolved map, not the configured one**, so a line written as a pin
/// reports the index it will actually be uploaded with. Before a board has been
/// attached the two are the same and the index is absent, which is a different
/// thing from being zero.
fn line_map_view(device: &StatemachinedDevice) -> wire::LineMapView {
    let map = &device.resolved_line_map;
    let pin_map = &device.pin_map;
    wire::LineMapView {
        input_lines: map
            .input_lines
            .iter()
            .map(|line| wire::InputLine {
                name: line.name.clone(),
                line_index: line.line_index.map(|index| index as i32),
                // The board's own name where it answered, and what the config
                // said otherwise -- never a blank where a person could have
                // been told something.
                pin_label: match line.line_index {
                    Some(index) if !pin_map.label_for(Direction::In, index).is_empty() => {
                        pin_map.label_for(Direction::In, index).to_string()
                    }
                    _ => line.pin_label.clone(),
                },
                reads_active_low: line.reads_active_low,
                is_enabled: line.is_enabled,
                debounce_milliseconds: line.debounce_milliseconds as i32,
                // Absent rather than false: the level is read from the device's
                // own `io` word, and nothing has read one yet. Absent is not
                // the same as low.
                is_high_now: None,
            })
            .collect(),
        output_lines: map
            .output_lines
            .iter()
            .map(|line| wire::OutputLine {
                name: line.name.clone(),
                line_index: line.line_index.map(|index| index as i32),
                pin_label: match line.line_index {
                    Some(index) if !pin_map.label_for(Direction::Out, index).is_empty() => {
                        pin_map.label_for(Direction::Out, index).to_string()
                    }
                    _ => line.pin_label.clone(),
                },
                safe_level_is_high: line.safe_level_is_high,
                is_high_now: None,
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
        unported!("State/read_state")
    }

    async fn watch_state(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::WatchStateRequest>,
    ) -> Result<tonic::Response<Self::WatchStateStream>, tonic::Status> {
        unported!("State/watch_state")
    }

    async fn read_trace(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadTraceRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::TraceWindow>, tonic::Status> {
        unported!("State/read_trace")
    }

    async fn watch_trace(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::WatchTraceRequest>,
    ) -> Result<tonic::Response<Self::WatchTraceStream>, tonic::Status> {
        unported!("State/watch_trace")
    }

    async fn read_trial_trace(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadTrialTraceRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::TrialTrace>, tonic::Status> {
        unported!("State/read_trial_trace")
    }

    async fn read_observers(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadObserversRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::Observers>, tonic::Status> {
        unported!("State/read_observers")
    }

}

// -- Trial ---------------------------------------------------------------------
#[tonic::async_trait]
impl service::trial_server::Trial for DaemonServices {
    async fn configure(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ConfigureTrialRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::ConfigureTrialResult>, tonic::Status> {
        unported!("Trial/configure")
    }

    async fn start(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::StartTrialRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::StartTrialResult>, tonic::Status> {
        unported!("Trial/start")
    }

    async fn cancel(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::CancelTrialRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::CancelTrialResult>, tonic::Status> {
        unported!("Trial/cancel")
    }

    async fn read_result(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadTrialResultRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::TrialResult>, tonic::Status> {
        unported!("Trial/read_result")
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
        let device = self.state.device.lock().map_err(poisoned)?;
        Ok(tonic::Response::new(device_state_to_wire(&device)))
    }

    async fn open_link(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::OpenLinkRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::DeviceState>, tonic::Status> {
        // Greet, which is what **takes the rig** from a board that was arming
        // its own trials. Opening the port does not do that; the greeting does,
        // and it is the whole point of the handover that a serial monitor
        // cannot trigger it.
        let mut device = self.state.device.lock().map_err(poisoned)?;
        device.connect_and_greet().map_err(status_for_device)?;
        Ok(tonic::Response::new(device_state_to_wire(&device)))
    }

    async fn read_lines(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadLinesRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::LineMapView>, tonic::Status> {
        let device = self.state.device.lock().map_err(poisoned)?;
        Ok(tonic::Response::new(line_map_view(&device)))
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
        unported!("Device/read_firmware")
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
        let name = request.into_inner().name;
        self.state.graphs.delete(&name).map_err(graph_problem)?;
        Ok(tonic::Response::new(graph_summaries(&self.state.graphs)))
    }

    async fn validate_graph(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::GraphValidation>, tonic::Status> {
        unported!("GraphStore/validate_graph")
    }

    async fn validate_graph_file(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::FileDraft>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::GraphValidation>, tonic::Status> {
        unported!("GraphStore/validate_graph_file")
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
                line_map: Some(line_map_view(&device)),
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
            let config = state.require_state_machine_config()?;
            let (compiled, elapsed) = state.upload_graph_set(&config.graphs)?;
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
        // Python also cancels a trial still armed, because an armed trial with
        // nobody driving it will run at whatever time somebody next touches a
        // lever. This daemon cannot arm one yet — `Trial/*` is not ported — so
        // there is nothing to cancel and `cancelled_trial_id` is always absent.
        // **The trial port must add the cancel here.**
        let was_open = self
            .state
            .session_opened_at
            .lock()
            .map_err(poisoned)?
            .take()
            .is_some();
        Ok(tonic::Response::new(wire::CloseSessionResult {
            was_open,
            cancelled_trial_id: None,
            session: Some(self.state.session_state()?),
        }))
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
