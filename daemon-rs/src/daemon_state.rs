// SPDX-License-Identifier: AGPL-3.0-or-later
//! What the daemon knows, shared by every rpc.
//!
//! The Rust counterpart of `daemon/api/rig_service.py` — 817 lines there, and
//! this will grow towards it. It is deliberately the *same* object behind every
//! service, because there is one rig: a `Session` rpc and a `Device` rpc that
//! held separate state would disagree about whether a board is open.

use std::sync::Mutex;

use crate::model::graph_definition::GraphDefinition;
pub use crate::rig_configuration::RigConfiguration;
use crate::model::state_machine_config::StateMachineConfig;
use crate::store::Store;

/// The rig, as much of it as is ported.
pub struct DaemonState {
    pub configuration: RigConfiguration,
    pub graphs: Store<GraphDefinition>,
    pub configs: Store<StateMachineConfig>,
    /// Which state-machine config this rig is loaded with, if any.
    ///
    /// A `Mutex` rather than an atomic because it is a name: `LoadConfig` sets
    /// it and every listing reports it, and both are far away from any hot path.
    pub loaded_config: Mutex<Option<String>>,
}

impl DaemonState {
    pub fn new(configuration: RigConfiguration) -> Self {
        Self {
            graphs: Store::new(configuration.graph_store_directory.clone()),
            configs: Store::new(configuration.state_machine_config_directory.clone()),
            loaded_config: Mutex::new(None),
            configuration,
        }
    }

    /// The name of the loaded config, or empty — which is how the proto spells
    /// "none", because a summary listing has no other way to say it.
    pub fn loaded_config_name(&self) -> String {
        self.loaded_config
            .lock()
            .expect("the loaded-config name is never held across a panic")
            .clone()
            .unwrap_or_default()
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
