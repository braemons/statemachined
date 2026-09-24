// SPDX-License-Identifier: AGPL-3.0-or-later
//! statemachined, in Rust.
//!
//! **The port of `daemon/`, not yet its replacement.** Every rpc answers, and
//! answers as the Python daemon does — `tools/compare_daemons.py` holds the two
//! to each other, and `make e2e-rust` runs the family's acceptance suite
//! against this one. The Python daemon stays the one on rigs until this one has
//! run a real session (`dev/RUST_PORT.md` §5.2, §8.1).
//!
//! The layering is mousewheeld's, because a braemons developer who has read
//! that daemon should be able to read this one:
//!
//! * `wire/` — the generated protobuf types. Nothing outside `grpc/` and
//!   `convert/` names one.
//! * `model/` — the shapes this daemon thinks in, and the documents on disk.
//! * `grpc/` — the rpcs.
//! * `web/` — the panels, on the same port.

pub mod daemon_state;
pub mod device;
pub mod event_recording;
pub mod firmware_manifest;
pub mod graph_set_compiler;
pub mod grpc;
pub mod mdns_service_advertisement;
pub mod native_device_on_a_socket;
pub mod model;
pub mod observer_registry;
pub mod rig_configuration;
pub mod store;
pub mod web;
pub mod wire;
