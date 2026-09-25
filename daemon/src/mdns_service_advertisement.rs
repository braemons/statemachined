// SPDX-License-Identifier: AGPL-3.0-or-later
//! Telling the network this rig exists.
//!
//! `_statemachined._tcp`, beside vstimd's `_vstimd._tcp` and triald's, so one
//! browse of the local domain finds every braemons daemon on a rig. A Pi whose
//! hostname is generated at boot cannot be hand-configured into a console's URL
//! list, and two rigs called `raspberrypi.local` is the normal case.
//!
//! **The identity is not the hostname.** `/etc/machine-id` survives a rename
//! and is different on every box, so a console that has seen this rig before
//! recognises it afterwards. It is hashed rather than published — a machine-id
//! is meant to be confidential — and hashed exactly as the Python daemon hashes
//! it, so a rig keeps its identity across the cutover.
//!
//! **Advertising is never fatal.** A daemon whose network is down still owns a
//! device and still serves an API somebody may reach by IP, so every failure
//! here is reported and swallowed, and `is_advertising` says which happened.

use std::collections::HashMap;
use std::path::Path;

use sha2::{Digest, Sha256};

pub const SERVICE_TYPE: &str = "_statemachined._tcp.local.";
pub const MACHINE_ID_PATH: &str = "/etc/machine-id";

/// Sixteen hex digits that mean "this box", across renames and reboots.
///
/// Falls back to the hostname where there is no machine-id — a container, a
/// developer's checkout. Weaker, and better than a value that changes every
/// restart, which would make a console show one rig as many.
pub fn stable_rig_identifier(machine_id_path: &Path) -> String {
    let seed = std::fs::read_to_string(machine_id_path)
        .map(|text| text.trim().to_string())
        .unwrap_or_default();
    let seed = if seed.is_empty() { hostname() } else { seed };
    let digest = Sha256::digest(format!("statemachined:{seed}").as_bytes());
    digest.iter().map(|byte| format!("{byte:02x}")).collect::<String>()[..16].to_string()
}

/// The box rather than this daemon on it: the same machine-id, salted
/// `braemons:`, and so the same sixteen digits in every braemons daemon's
/// record on one rig. `id` cannot do this — it is salted with the daemon's own
/// name by design — and a console grouping on it shows each rig once per
/// daemon (`console/docs/PLAN.md` §4).
pub fn rig_identifier(machine_id_path: &Path) -> String {
    let seed = std::fs::read_to_string(machine_id_path)
        .map(|text| text.trim().to_string())
        .unwrap_or_default();
    let seed = if seed.is_empty() { hostname() } else { seed };
    let digest = Sha256::digest(format!("braemons:{seed}").as_bytes());
    digest.iter().map(|byte| format!("{byte:02x}")).collect::<String>()[..16].to_string()
}

/// What a console can act on without opening a connection first.
///
/// Deliberately small: a TXT record is not an API. `elements` is here because
/// the panels' URL is one a console has to construct, and constructing it from
/// a convention rather than from the record is how a console breaks behind a
/// proxy. `api` is carried as the Python daemon carries it, though nothing
/// reads it (`dev/RUST_PORT_STATUS.md`, open questions).
pub fn text_records_for(
    rig_identifier: &str,
    port: u16,
    device_target: &str,
    version: &str,
) -> HashMap<String, String> {
    [
        ("id", rig_identifier.to_string()),
        ("version", version.to_string()),
        ("api", "/api".to_string()),
        ("elements", "/elements/statemachined.js".to_string()),
        ("device", device_target.to_string()),
        ("port", port.to_string()),
    ]
    .into_iter()
    .map(|(key, value)| (key.to_string(), value))
    .collect()
}

/// This box's name, as somebody standing next to the rig would say it.
pub fn hostname() -> String {
    let mut buffer = [0u8; 256];
    // SAFETY: a buffer and its length, and gethostname writes within it.
    let written = unsafe { libc::gethostname(buffer.as_mut_ptr().cast(), buffer.len()) };
    if written != 0 {
        return "statemachined".into();
    }
    let end = buffer.iter().position(|byte| *byte == 0).unwrap_or(buffer.len());
    String::from_utf8_lossy(&buffer[..end]).to_string()
}

/// One `_statemachined._tcp` registration, for as long as the daemon runs.
///
/// Withdrawn with it, because a record that outlives its daemon is worse than
/// none: a console shows a rig that cannot answer.
pub struct MdnsServiceAdvertisement {
    pub port: u16,
    pub device_target: String,
    pub version: String,
    /// The hostname's first label when empty.
    pub instance_name: String,
    pub is_advertising: bool,
    pub last_failure: String,
    daemon: Option<mdns_sd::ServiceDaemon>,
    fullname: Option<String>,
}

impl MdnsServiceAdvertisement {
    pub fn new(port: u16, device_target: &str, version: &str) -> Self {
        Self {
            port,
            device_target: device_target.to_string(),
            version: version.to_string(),
            instance_name: String::new(),
            is_advertising: false,
            last_failure: String::new(),
            daemon: None,
            fullname: None,
        }
    }

    fn short_hostname() -> String {
        hostname().split('.').next().unwrap_or_default().to_string()
    }

    /// `<instance>._statemachined._tcp.local.` — the hostname by default.
    /// Collisions are the responder's problem, and the `id` record is what
    /// survives its answer.
    pub fn service_name(&self) -> String {
        let instance = if self.instance_name.is_empty() {
            Self::short_hostname()
        } else {
            self.instance_name.clone()
        };
        format!("{instance}.{SERVICE_TYPE}")
    }

    /// The record, as the responder wants it. Separate so a test can read it.
    pub fn build_service_info(&self) -> Result<mdns_sd::ServiceInfo, String> {
        let instance = self.service_name();
        let instance = instance.trim_end_matches(&format!(".{SERVICE_TYPE}"));
        let mut records = text_records_for(
            &stable_rig_identifier(Path::new(MACHINE_ID_PATH)),
            self.port,
            &self.device_target,
            &self.version,
        );
        records.insert("rig".into(), rig_identifier(Path::new(MACHINE_ID_PATH)));
        mdns_sd::ServiceInfo::new(
            SERVICE_TYPE,
            instance,
            &format!("{}.local.", Self::short_hostname()),
            "",
            self.port,
            records,
        )
        .map(|info| info.enable_addr_auto())
        .map_err(|problem| problem.to_string())
    }

    /// Register. Returns whether it worked; never fails the daemon.
    pub fn start(&mut self) -> bool {
        let registered = (|| {
            let info = self.build_service_info()?;
            let daemon = mdns_sd::ServiceDaemon::new().map_err(|problem| problem.to_string())?;
            let fullname = info.get_fullname().to_string();
            daemon.register(info).map_err(|problem| problem.to_string())?;
            Ok::<_, String>((daemon, fullname))
        })();
        match registered {
            Ok((daemon, fullname)) => {
                self.daemon = Some(daemon);
                self.fullname = Some(fullname);
                self.is_advertising = true;
                true
            }
            Err(problem) => {
                self.last_failure = problem;
                log::warn!(
                    "mDNS: not advertising ({}). The API is still served; a console will need \
                     the address by hand.",
                    self.last_failure
                );
                false
            }
        }
    }

    /// Withdraw the record, then close. Also never fails.
    pub fn stop(&mut self) {
        if let (Some(daemon), Some(fullname)) = (&self.daemon, &self.fullname) {
            if let Err(problem) = daemon.unregister(fullname) {
                self.last_failure = problem.to_string();
            }
        }
        if let Some(daemon) = self.daemon.take() {
            let _ = daemon.shutdown();
        }
        self.fullname = None;
        self.is_advertising = false;
    }
}

impl Drop for MdnsServiceAdvertisement {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_identifier_is_stable_and_is_not_the_machine_id() {
        let directory = tempfile::tempdir().unwrap();
        let machine_id = directory.path().join("machine-id");
        std::fs::write(&machine_id, "dc4f4b06f2d84f6b9e2a7b0c1d2e3f40\n").unwrap();
        let identifier = stable_rig_identifier(&machine_id);
        assert_eq!(identifier, stable_rig_identifier(&machine_id));
        assert_eq!(identifier.len(), 16);
        assert!(!identifier.contains("dc4f4b06"));
    }

    #[test]
    fn the_identifier_is_the_one_the_python_daemon_gave_this_rig() {
        // Computed by `stable_rig_identifier` in Python for this machine-id. A
        // console recognises a rig by it; changing daemons must not rename it.
        let directory = tempfile::tempdir().unwrap();
        let machine_id = directory.path().join("machine-id");
        std::fs::write(&machine_id, "dc4f4b06f2d84f6b9e2a7b0c1d2e3f40\n").unwrap();
        assert_eq!(stable_rig_identifier(&machine_id), "0b60208b6da842d7");
    }

    #[test]
    fn the_rig_identifier_is_the_family_one() {
        // mousewheeld's test asserts the same salting; this is the value both
        // daemons advertise as `rig=` for this machine-id.
        let directory = tempfile::tempdir().unwrap();
        let machine_id = directory.path().join("machine-id");
        std::fs::write(&machine_id, "dc4f4b06f2d84f6b9e2a7b0c1d2e3f40\n").unwrap();
        let rig = rig_identifier(&machine_id);
        assert_eq!(rig.len(), 16);
        assert_ne!(rig, stable_rig_identifier(&machine_id));
    }

    #[test]
    fn two_boxes_get_different_identifiers() {
        let directory = tempfile::tempdir().unwrap();
        std::fs::write(directory.path().join("one"), "1".repeat(32)).unwrap();
        std::fs::write(directory.path().join("two"), "2".repeat(32)).unwrap();
        assert_ne!(
            stable_rig_identifier(&directory.path().join("one")),
            stable_rig_identifier(&directory.path().join("two"))
        );
    }

    #[test]
    fn a_box_with_no_machine_id_still_gets_one() {
        let directory = tempfile::tempdir().unwrap();
        assert_eq!(stable_rig_identifier(&directory.path().join("absent")).len(), 16);
    }

    #[test]
    fn the_text_records_say_where_the_api_and_the_elements_are() {
        let records = text_records_for("abc123", 8081, "/dev/ttyACM0", "1.2.3");
        assert_eq!(records["id"], "abc123");
        assert_eq!(records["api"], "/api");
        assert_eq!(records["elements"], "/elements/statemachined.js");
        assert_eq!(records["device"], "/dev/ttyACM0");
        assert_eq!(records["version"], "1.2.3");
    }

    #[test]
    fn the_service_name_is_under_the_braemons_service_type() {
        let mut advertisement = MdnsServiceAdvertisement::new(8081, "", "0");
        advertisement.instance_name = "rig-3".into();
        assert_eq!(advertisement.service_name(), format!("rig-3.{SERVICE_TYPE}"));
    }

    #[test]
    fn the_record_carries_the_port_it_was_told_to_advertise() {
        let mut advertisement = MdnsServiceAdvertisement::new(9099, "", "0");
        advertisement.instance_name = "rig-3".into();
        let info = advertisement.build_service_info().unwrap();
        assert_eq!(info.get_port(), 9099);
        assert_eq!(info.get_property_val_str("port"), Some("9099"));
    }

    #[test]
    fn stopping_something_that_never_started_is_not_a_failure() {
        let mut advertisement = MdnsServiceAdvertisement::new(8081, "", "0");
        advertisement.stop();
        assert!(!advertisement.is_advertising);
        assert!(advertisement.last_failure.is_empty());
    }
}
