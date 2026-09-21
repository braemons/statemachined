// SPDX-License-Identifier: AGPL-3.0-or-later
//! What the daemon knows, shared by every rpc.
//!
//! The Rust counterpart of `daemon/api/rig_service.py` — 817 lines there, and
//! this will grow towards it. It is deliberately the *same* object behind every
//! service, because there is one rig: a `Session` rpc and a `Device` rpc that
//! held separate state would disagree about whether a board is open.

use std::sync::Mutex;

use crate::device::statemachined_device::StatemachinedDevice;
use crate::model::graph_definition::GraphDefinition;
use crate::model::line_map::LineMap;
pub use crate::rig_configuration::RigConfiguration;
use crate::model::state_machine_config::StateMachineConfig;
use crate::store::Store;

/// The rig, as much of it as is ported.
pub struct DaemonState {
    /// The board, and the link to it.
    ///
    /// A `Mutex` and not a lock-free thing: **the device is serialised by
    /// definition.** The protocol is one command in flight, so two rpcs that
    /// both want the board must queue, and the queue is the honest shape for
    /// it. The Python daemon calls the same thing `device_lock`.
    pub device: Mutex<StatemachinedDevice>,
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
        let mut device =
            StatemachinedDevice::new(configuration.device_target.clone(), LineMap::default());
        device.baud = configuration.device_baud.max(0) as u32;
        device.timeout = std::time::Duration::from_secs_f64(
            configuration.device_timeout_seconds.max(0.0),
        );
        device.expected_board = configuration.expected_board.clone();
        device.configured_session_seed = if configuration.session_seed.is_empty() {
            None
        } else {
            Some(configuration.session_seed.clone())
        };
        Self {
            device: Mutex::new(device),
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
    /// a fact the API reports rather than a reason to refuse — and why it is
    /// answered without touching the board, so it stays cheap enough to poll
    /// and cannot be made to hang by a serial port that is not draining.
    pub fn device_connected(&self) -> bool {
        self.device
            .lock()
            .map(|device| device.is_connected())
            .unwrap_or(false)
    }
}
