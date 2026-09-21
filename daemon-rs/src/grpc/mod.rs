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
//! has (`RUST_REWRITE_PLAN.md` §5.2: there is no split where both run).

use std::sync::Arc;

use crate::daemon_state::DaemonState;

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
        unported!("Device/read_device")
    }

    async fn open_link(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::OpenLinkRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::DeviceState>, tonic::Status> {
        unported!("Device/open_link")
    }

    async fn read_lines(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadLinesRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::LineMapView>, tonic::Status> {
        unported!("Device/read_lines")
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
        unported!("GraphStore/list_graphs")
    }

    async fn read_graph_file(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::StoredFile>, tonic::Status> {
        unported!("GraphStore/read_graph_file")
    }

    async fn write_graph_file(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::StoredFile>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::GraphSummary>, tonic::Status> {
        unported!("GraphStore/write_graph_file")
    }

    async fn delete_graph(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::DeleteFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::GraphSummaries>, tonic::Status> {
        unported!("GraphStore/delete_graph")
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
        unported!("StateMachineConfigStore/list_configs")
    }

    async fn read_config_file(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::ReadFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::StoredFile>, tonic::Status> {
        unported!("StateMachineConfigStore/read_config_file")
    }

    async fn write_config_file(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::StoredFile>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::StateMachineConfigSummaries>, tonic::Status> {
        unported!("StateMachineConfigStore/write_config_file")
    }

    async fn delete_config(
        &self,
        _request: tonic::Request<crate::wire::statemachined::v1::DeleteFileRequest>,
    ) -> Result<tonic::Response<crate::wire::statemachined::v1::StateMachineConfigSummaries>, tonic::Status> {
        unported!("StateMachineConfigStore/delete_config")
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
        unported!("Configuration/read_configuration")
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
