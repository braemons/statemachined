// SPDX-License-Identifier: AGPL-3.0-or-later
//! statemachined, in Rust.
//!
//! **A port in progress.** `daemon/` holds the Python daemon that runs on rigs;
//! this is what replaces it, and until every rpc is ported the two are not
//! interchangeable — see `dev/RUST_PORT.md`. `grpc/mod.rs` is the
//! scoreboard: what is not ported answers `UNIMPLEMENTED` and says so.
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
pub mod graph_set_compiler;
pub mod grpc;
pub mod model;
pub mod rig_configuration;
pub mod store;
pub mod web;
pub mod wire;
