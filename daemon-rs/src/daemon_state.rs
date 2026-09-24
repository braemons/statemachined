// SPDX-License-Identifier: AGPL-3.0-or-later
//! What the daemon knows, shared by every rpc.
//!
//! The Rust counterpart of `daemon/api/rig_service.py` — 817 lines there, and
//! this will grow towards it. It is deliberately the *same* object behind every
//! service, because there is one rig: a `Session` rpc and a `Device` rpc that
//! held separate state would disagree about whether a board is open.

use std::sync::Mutex;

use crate::device::state_visit_trace::StateVisitTrace;
use crate::device::statemachined_device::StatemachinedDevice;
use crate::observer_registry::ObserverRegistry;
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
    /// What this rig is wired like and what it can run, or `None` before
    /// anybody has said.
    ///
    /// **The document, not its name.** A session opens over the graphs that
    /// were loaded, and re-reading the store at `Open` would open over whatever
    /// somebody saved since — a line map from Tuesday and graphs from Thursday.
    /// `None` is a real state: a rig on a bench in the morning, board plugged
    /// in, experiment not yet chosen.
    pub loaded_config: Mutex<Option<StateMachineConfig>>,
    /// When the graphs went up, as the session's own record of itself.
    ///
    /// `None` means no session is open — the board may still hold a committed
    /// set from before, which `ReadSession` says plainly rather than pretending
    /// either way.
    pub session_opened_at: Mutex<Option<std::time::SystemTime>>,
    /// Which graph a trial gets when it does not name one.
    ///
    /// A **default, not a mode**: an explicit graph on `Configure` always
    /// wins, so triald — which names a graph per trial and has no reason to
    /// know this exists — is unaffected by whatever somebody selected in a
    /// browser tab.
    pub active_graph: Mutex<Option<String>>,
    /// Everything that happened, in order: the ring the API reads, and the
    /// day's file it never reads back.
    pub trace: StateVisitTrace,
    /// Who is watching, while they are watching. Never read by this daemon.
    pub observers: ObserverRegistry,
    /// The last thing that went wrong with the board where nobody was waiting
    /// for an answer — a startup connect, a startup config. Reported, never
    /// fatal: a daemon that refused to start without a board would take the
    /// API down exactly when somebody needs it to find out why.
    pub last_error_from_the_device: Mutex<Option<String>>,
    /// The last trial to complete. One, not a history: the trace is the
    /// history, and `ReadTrialTrace` is how a caller asks about any other.
    pub last_trial_result: Mutex<Option<crate::model::trial_record::TrialResultRecord>>,
    /// The device's own per-run sequence number, to notice a dropped visit.
    pub last_visit_sequence_number: Mutex<Option<i64>>,
    /// Set to stop the link thread.
    pub stopping: std::sync::atomic::AtomicBool,
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
            session_opened_at: Mutex::new(None),
            active_graph: Mutex::new(None),
            trace: StateVisitTrace::new(
                configuration.trace_ring_entries.max(0) as usize,
                Some(configuration.trace_directory.clone()),
            ),
            observers: ObserverRegistry::new(),
            last_error_from_the_device: Mutex::new(None),
            last_trial_result: Mutex::new(None),
            last_visit_sequence_number: Mutex::new(None),
            stopping: std::sync::atomic::AtomicBool::new(false),
            configuration,
        }
    }

    /// The name of the loaded config, or empty — which is how the proto spells
    /// "none", because a summary listing has no other way to say it.
    pub fn loaded_config_name(&self) -> String {
        self.loaded_config
            .lock()
            .expect("the loaded-config name is never held across a panic")
            .as_ref()
            .map(|config| config.name.clone())
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
