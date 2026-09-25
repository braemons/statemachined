// SPDX-License-Identifier: AGPL-3.0-or-later
//! Everything the device needs to be a working state machine for an experiment.
//!
//! The other half of the rig configuration, and the split is the same one vstimd
//! makes between its rig-config and its scene-config: **what the box is**
//! against **what the box is doing today**.
//!
//! * The rig config is hand-edited TOML in `/etc/braemons`, a package conffile,
//!   and the daemon never writes it — things true of the box in the rack
//!   whatever experiment is running on it.
//! * A state-machine config is this: the line map and the graphs, written by the
//!   web UI, saved under `/var/lib/braemons/statemachined/configs/`. It is what
//!   a person changes on a Tuesday, so it lives where a daemon may write and a
//!   package upgrade will not tread.
//!
//! **Self-contained, and that is a decision rather than an accident.** The
//! graphs are *in* here, not named and fetched from the store. What that buys is
//! a file somebody can hand to a colleague, archive beside the session's data,
//! or diff against the config that was running the week the numbers changed —
//! which is the question this file will actually be asked, months later.
//!
//! **The price is that a config carries a line map, and a line map is per box.**
//! A graph is portable because it names `reward_valve`; the map naming which pin
//! that is describes one rig and no other. What makes that survivable is the
//! pin-only form: the map names `A0`, the daemon resolves it against the board
//! the moment the config is loaded, and a board that has not got that pin is
//! refused with the board's own pins in the message. Loud, at load, before a
//! valve moves.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use super::graph_definition::{refuse, GraphDefinition, Refused, Result as Checked};
use super::line_map::LineMap;

/// What a config may be called on disk.
///
/// A name is a file name, so it may not climb out of the directory or hide
/// itself, and it is reachable from a request parameter — which is exactly the
/// place not to trust one. Written out rather than as a regex crate dependency:
/// the rule is "starts alphanumeric, then alphanumerics and `.`, `_`, `-`".
pub fn name_is_allowed(name: &str) -> bool {
    let mut characters = name.chars();
    let Some(first) = characters.next() else {
        return false;
    };
    if !first.is_ascii_alphanumeric() {
        return false;
    }
    characters.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

/// One experiment's wiring and paradigms, as a person saved them.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StateMachineConfig {
    pub name: String,
    /// Free text for the person who opens this in six months. Not used for
    /// anything, which is the point: a field nothing parses is a field nobody
    /// has to keep in a format.
    #[serde(default)]
    pub description: String,
    /// The board this was authored against, as `hello_ack` names it. Advisory
    /// and checked at load: a config written for one board and loaded on another
    /// is the case the line map cannot always catch on its own, because two
    /// boards can both have a pin called `A0` and mean different holes. Empty
    /// means "did not say", which is not an error.
    #[serde(default)]
    pub board: String,
    /// Which pin is the left lever, in the box this was saved on.
    #[serde(default)]
    pub line_map: LineMap,
    /// Every graph a session using this config may run, in the order they will
    /// take slots on the device. The whole set goes up before the first trial,
    /// so this list is what a session *is*.
    #[serde(default)]
    pub graphs: Vec<GraphDefinition>,
}

impl StateMachineConfig {
    /// Parse and check, which is the only way to get one.
    pub fn from_json(text: &str) -> Checked<Self> {
        let config: Self = serde_json::from_str(text).map_err(|p| Refused(p.to_string()))?;
        config.validate()?;
        Ok(config)
    }

    pub fn validate(&self) -> Checked<()> {
        if self.name.is_empty() || self.name.len() > 128 || !name_is_allowed(&self.name) {
            return refuse(format!("'{}' is not a usable config name", self.name));
        }
        self.line_map.validate()?;
        let mut seen: BTreeSet<&str> = BTreeSet::new();
        for graph in &self.graphs {
            graph.validate_document()?;
            if !seen.insert(graph.name.as_str()) {
                // A trial names a graph, never a slot, so two graphs of one
                // name is a trial whose paradigm depends on which copy the
                // compiler reached first.
                return refuse(format!(
                    "two graphs in this config are called '{}'",
                    graph.name
                ));
            }
        }
        Ok(())
    }

    /// One graph, or a refusal naming what this config holds.
    pub fn graph_named(&self, graph_name: &str) -> Checked<&GraphDefinition> {
        self.graphs
            .iter()
            .find(|graph| graph.name == graph_name)
            .ok_or_else(|| {
                let known: Vec<&str> = self.graphs.iter().map(|g| g.name.as_str()).collect();
                Refused(format!(
                    "no graph called '{graph_name}' is in the state-machine config '{}'. \
                     It has: {}",
                    self.name,
                    if known.is_empty() { "(none)".to_string() } else { known.join(", ") }
                ))
            })
    }
}
