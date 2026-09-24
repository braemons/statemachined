// SPDX-License-Identifier: AGPL-3.0-or-later
//! statemachined, in Rust.
//!
//! **The port of the Python daemon, which it has replaced.** Every rpc answered
//! as the Python daemon did — `tools/compare_daemons.py` held the two to each
//! other until the board's link moved to protobuf and the Python daemon was
//! retired — and `make e2e` runs the family's acceptance suite against this one.
//! It has not yet run a real session on a rig (`dev/RUST_PORT.md` §8.1).
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
