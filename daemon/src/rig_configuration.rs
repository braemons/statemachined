// SPDX-License-Identifier: AGPL-3.0-or-later
//! One box's settings: what the rig *is*, as against what it is doing today.
//!
//! Hand-edited TOML in `/etc/braemons`, a package conffile, and **the daemon
//! never writes it**. The other half — the line map and the graphs, the things
//! somebody adjusts at the bench on a Tuesday — is a state-machine config under
//! `/var/lib/braemons/statemachined/configs/`. The reason for the split is
//! mechanical rather than aesthetic: a conffile the daemon rewrites is a file
//! that fights dpkg on every upgrade.
//!
//! **The paths are the package's, and a bench passes its own.** There is no
//! search order and no guessing at who started the process: the defaults here
//! are where the `.deb` puts things, `--rig-config` and the directory settings
//! override them. A daemon that answered "which file am I reading" differently
//! depending on `$HOME` is a daemon nobody can debug over the phone.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

pub const DEFAULT_CONFIGURATION_PATH: &str = "/etc/braemons/statemachined-rig-config.toml";

/// Everything this daemon writes, under one directory the package owns and
/// systemd's `StateDirectory=` creates. One place to back up beats three.
pub const DEFAULT_STATE_DIRECTORY: &str = "/var/lib/braemons/statemachined";

/// How a session's graphs reach the board.
///
/// `set` uploads every graph once and selects per trial; `per_trial` uploads
/// one each time. The first is how a session runs and the second is how a board
/// too small for the set still runs it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GraphMode {
    Set,
    PerTrial,
}

impl GraphMode {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Set => "set",
            Self::PerTrial => "per_trial",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RigConfiguration {
    /// A device path, a `host:port`, or any pyserial URL. One device per daemon;
    /// a rig with two MCUs is a systemd template with a config per instance.
    #[serde(default = "default_device_target")]
    pub device_target: String,
    #[serde(default = "default_baud")]
    pub device_baud: i32,
    #[serde(default = "default_device_timeout")]
    pub device_timeout_seconds: f64,
    /// Refuse a board that is not this one. Empty accepts whatever answers,
    /// which is right for a bench and wrong for a rig with four of them on a
    /// bench beside it.
    #[serde(default)]
    pub expected_board: String,
    /// Fixed for the life of the daemon when set, so that a whole session —
    /// including one interrupted by a reconnect — replays. Empty means one is
    /// drawn per connection, which is right for a rig and wrong for a
    /// reproduction.
    #[serde(default)]
    pub session_seed: String,
    #[serde(default = "yes")]
    pub connect_on_startup: bool,
    /// A config to load when the daemon starts, so a rig comes back up loaded
    /// rather than waiting for somebody to notice.
    #[serde(default)]
    pub startup_state_machine_config: String,
    #[serde(default = "default_graph_mode")]
    pub graph_mode: GraphMode,
    #[serde(default = "default_trace_ring_entries")]
    pub trace_ring_entries: i32,
    #[serde(default = "default_heartbeat")]
    pub heartbeat_seconds: f64,
    #[serde(default = "default_trace_directory")]
    pub trace_directory: PathBuf,
    #[serde(default = "default_graph_directory")]
    pub graph_store_directory: PathBuf,
    /// Empty means nothing is recorded, which is right for a bench and wrong
    /// for a rig — so this says which it is rather than failing at the first
    /// `Recording/Start`.
    #[serde(default = "default_recording_directory")]
    pub recording_directory: PathBuf,
    #[serde(default = "default_config_directory")]
    pub state_machine_config_directory: PathBuf,
}

fn default_device_target() -> String {
    "/dev/braemons/statemachined0".into()
}
fn default_baud() -> i32 {
    115_200
}
fn default_device_timeout() -> f64 {
    2.0
}
fn yes() -> bool {
    true
}
fn default_graph_mode() -> GraphMode {
    GraphMode::Set
}
fn default_trace_ring_entries() -> i32 {
    100_000
}
fn default_heartbeat() -> f64 {
    2.0
}
fn state_directory() -> PathBuf {
    PathBuf::from(DEFAULT_STATE_DIRECTORY)
}
fn default_trace_directory() -> PathBuf {
    state_directory().join("trace")
}
fn default_graph_directory() -> PathBuf {
    state_directory().join("graphs")
}
fn default_recording_directory() -> PathBuf {
    state_directory().join("recordings")
}
fn default_config_directory() -> PathBuf {
    state_directory().join("configs")
}

impl Default for RigConfiguration {
    fn default() -> Self {
        Self {
            device_target: default_device_target(),
            device_baud: default_baud(),
            device_timeout_seconds: default_device_timeout(),
            expected_board: String::new(),
            session_seed: String::new(),
            connect_on_startup: yes(),
            startup_state_machine_config: String::new(),
            graph_mode: default_graph_mode(),
            trace_ring_entries: default_trace_ring_entries(),
            heartbeat_seconds: default_heartbeat(),
            trace_directory: default_trace_directory(),
            graph_store_directory: default_graph_directory(),
            recording_directory: default_recording_directory(),
            state_machine_config_directory: default_config_directory(),
        }
    }
}

impl RigConfiguration {
    /// Read the TOML, or the built-in defaults when there is none.
    ///
    /// **Its absence is not an error.** A bench with no conffile runs on the
    /// defaults, which is what `--rig-config` pointing at nothing means too.
    pub fn read(path: &std::path::Path) -> Result<Self, String> {
        match std::fs::read_to_string(path) {
            Ok(text) => toml::from_str(&text)
                .map_err(|problem| format!("{}: {problem}", path.display())),
            Err(problem) if problem.kind() == std::io::ErrorKind::NotFound => {
                Ok(Self::default())
            }
            Err(problem) => Err(format!("{}: {problem}", path.display())),
        }
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.trace_ring_entries <= 0 {
            return Err(format!(
                "trace_ring_entries is {}, and a ring holds at least one entry",
                self.trace_ring_entries
            ));
        }
        if self.heartbeat_seconds <= 0.0 {
            return Err(format!(
                "heartbeat_seconds is {}, and a heartbeat has a period",
                self.heartbeat_seconds
            ));
        }
        Ok(())
    }
}
