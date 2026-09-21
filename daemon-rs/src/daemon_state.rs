// SPDX-License-Identifier: AGPL-3.0-or-later
//! What the daemon knows, shared by every rpc.
//!
//! The Rust counterpart of `daemon/api/rig_service.py` — 817 lines there, and
//! this will grow towards it. It is deliberately the *same* object behind every
//! service, because there is one rig: a `Session` rpc and a `Device` rpc that
//! held separate state would disagree about whether a board is open.

use std::path::PathBuf;

/// Where a rig's documents live, and how to reach its board.
///
/// The Rust counterpart of `rig_configuration.py`. Ported ahead of the things
/// that read it because every other piece needs somewhere to put its files.
#[derive(Debug, Clone)]
pub struct RigConfiguration {
    pub device_target: String,
    pub connect_on_startup: bool,
    pub graph_store_directory: PathBuf,
    pub state_machine_config_directory: PathBuf,
    pub trace_directory: PathBuf,
    pub recording_directory: PathBuf,
}

impl Default for RigConfiguration {
    fn default() -> Self {
        Self {
            device_target: "loop://".into(),
            connect_on_startup: false,
            graph_store_directory: PathBuf::from("/var/lib/braemons/statemachined/graphs"),
            state_machine_config_directory: PathBuf::from("/var/lib/braemons/statemachined/configs"),
            trace_directory: PathBuf::from("/var/lib/braemons/statemachined/trace"),
            recording_directory: PathBuf::from("/var/lib/braemons/statemachined/recordings"),
        }
    }
}

/// The rig, as much of it as is ported.
pub struct DaemonState {
    pub configuration: RigConfiguration,
}

impl DaemonState {
    pub fn new(configuration: RigConfiguration) -> Self {
        Self { configuration }
    }

    /// Whether a board is attached and its link is open.
    ///
    /// **A daemon with no board is not a broken daemon**, which is why this is
    /// a fact the API reports rather than a reason to refuse. Always false
    /// until the device layer is ported (`RUST_REWRITE_PLAN.md` §4.1).
    pub fn device_connected(&self) -> bool {
        false
    }
}
