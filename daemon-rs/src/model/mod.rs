// SPDX-License-Identifier: AGPL-3.0-or-later
//! The shapes this daemon thinks in, and the documents on disk.
//!
//! **These are user data.** A graph file somebody wrote last year lives in
//! `/var/lib/statemachined/graphs/` and must parse identically under this
//! daemon and the Python one — see `tests/documents.rs`, which runs both over
//! the same corpus and compares what each accepts, what each refuses, and what
//! each parses to.

pub mod graph_definition;
pub mod line_map;
pub mod state_machine_config;
pub mod trial_outcome;
