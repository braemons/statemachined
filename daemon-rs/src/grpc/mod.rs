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
use crate::model::graph_definition::{GraphDefinition, Refused};
use crate::model::state_machine_config::StateMachineConfig;
use crate::store::{Document, Store, StoreProblem};
use crate::wire::statemachined::v1 as wire;

use crate::wire::statemachined::v1::service;

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

fn status_for(problem: StoreProblem) -> tonic::Status {
    match problem {
        StoreProblem::NotStored(sentence) => tonic::Status::not_found(sentence),
        StoreProblem::BadName(sentence) | StoreProblem::Unreadable(Refused(sentence)) => {
            tonic::Status::invalid_argument(sentence)
        }
        StoreProblem::Io(sentence) => tonic::Status::internal(sentence),
    }
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
        ..Default::default()
    }
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
    match problem {
        // **`unavailable` means no board**, which is the one thing a panel may
        // retry. Everything else is a request to change something.
        DeviceProblem::NotConnected(sentence) => tonic::Status::unavailable(sentence),
        DeviceProblem::Link(problem) => tonic::Status::unavailable(problem.to_string()),
        DeviceProblem::WrongBoard(sentence) | DeviceProblem::LineMap(sentence) => {
            tonic::Status::failed_precondition(sentence)
        }
        DeviceProblem::Request(problem) => tonic::Status::unavailable(problem.to_string()),
    }
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
        let graph = self.state.graphs.load(&name).map_err(status_for)?;
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
            .map_err(status_for)?;
        Ok(tonic::Response::new(graph_summary(&self.state.graphs, &graph.name)))
    }

    async fn delete_graph(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::DeleteFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::GraphSummaries>, tonic::Status> {
        let name = request.into_inner().name;
        self.state.graphs.delete(&name).map_err(status_for)?;
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
        _request: tonic::Request<crate::wire::statemachined::v1::ReadFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::CommittedGraphSet>, tonic::Status> {
        unported!("GraphStore/upload_graph")
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
        let config = self.state.configs.load(&name).map_err(status_for)?;
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
            .map_err(status_for)?;
        Ok(tonic::Response::new(config_summaries(&self.state)))
    }

    async fn delete_config(
        &self,
        request: tonic::Request<crate::wire::statemachined::v1::DeleteFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::StateMachineConfigSummaries>, tonic::Status> {
        let name = request.into_inner().name;
        self.state.configs.delete(&name).map_err(status_for)?;
        Ok(tonic::Response::new(config_summaries(&self.state)))
    }

    async fn load_config(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::LoadedConfigResult>, tonic::Status> {
        unported!("StateMachineConfigStore/load_config")
    }

}

// -- Session -------------------------------------------------------------------
#[tonic::async_trait]
impl service::session_server::Session for DaemonServices {
    async fn read_session(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadSessionRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::SessionState>, tonic::Status> {
        unported!("Session/read_session")
    }

    async fn open(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::OpenSessionRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::OpenSessionResult>, tonic::Status> {
        unported!("Session/open")
    }

    async fn upload_graphs(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::UploadGraphsRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::OpenSessionResult>, tonic::Status> {
        unported!("Session/upload_graphs")
    }

    async fn close(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::CloseSessionRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::CloseSessionResult>, tonic::Status> {
        unported!("Session/close")
    }

    async fn set_active_graph(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::SetActiveGraphRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::ActiveGraph>, tonic::Status> {
        unported!("Session/set_active_graph")
    }

    async fn clear_active_graph(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ClearActiveGraphRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::ActiveGraph>, tonic::Status> {
        unported!("Session/clear_active_graph")
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
