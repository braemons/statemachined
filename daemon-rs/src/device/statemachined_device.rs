// SPDX-License-Identifier: AGPL-3.0-or-later
//! One MCU, for as long as the daemon is running.
//!
//! **A port in progress.** What is here is the connection lifecycle — opening a
//! link, greeting the board, checking it is the board this rig is wired for,
//! resolving the line map against its pins, and pushing the wiring — and the
//! graph-set upload. The trial loop and the result reassembly are not ported
//! yet and the rpcs that need them still answer `UNIMPLEMENTED`.
//!
//! The ordering in `connect_and_greet` is the part to keep: **the greeting is
//! what takes the rig** from a board that was arming its own trials, and the
//! wiring is what makes the board's fail-safe correct for *this* box — so it
//! goes before any graph and long before any trial.

use std::time::Duration;

use serde_json::Value;

use super::board_pin_labels;
use super::graph_set_upload::send_compiled_upload_messages;
use super::device_clock_correlation::DeviceClockCorrelation;
use super::device_pin_map::{Direction, DevicePinMap, PinLabelSource};
use super::message_vocabulary::MsgType;
use super::request_response_session::{
    random_seed, Discard, RequestProblem, RequestResponseSession,
};
use super::serial_link::SerialLink;
use crate::graph_set_compiler::{
    compile_graph_set_for_device, CompileError, CompiledGraphSet, DeviceCapabilities,
};
use crate::model::graph_definition::GraphDefinition;
use crate::model::line_map::LineMap;

/// What a caller did that needs a board, without one.
#[derive(Debug)]
pub enum DeviceProblem {
    NotConnected(String),
    /// This is not the board the rig config names.
    WrongBoard(String),
    /// The line map does not fit the board's pins.
    LineMap(String),
    Request(RequestProblem),
    Link(std::io::Error),
}

impl std::fmt::Display for DeviceProblem {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotConnected(s) | Self::WrongBoard(s) | Self::LineMap(s) => f.write_str(s),
            Self::Request(problem) => write!(f, "{problem}"),
            Self::Link(problem) => write!(f, "{problem}"),
        }
    }
}

impl From<RequestProblem> for DeviceProblem {
    fn from(problem: RequestProblem) -> Self {
        Self::Request(problem)
    }
}

/// Why a set did not reach the board: it would not compile against this
/// board, or the board (or the link to it) said no.
#[derive(Debug)]
pub enum UploadProblem {
    Compile(CompileError),
    Device(DeviceProblem),
}

impl std::fmt::Display for UploadProblem {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Compile(problem) => write!(f, "{problem}"),
            Self::Device(problem) => write!(f, "{problem}"),
        }
    }
}

impl<T: Into<DeviceProblem>> From<T> for UploadProblem {
    fn from(problem: T) -> Self {
        Self::Device(problem.into())
    }
}

impl From<std::io::Error> for DeviceProblem {
    fn from(problem: std::io::Error) -> Self {
        Self::Link(problem)
    }
}

/// How this daemon reaches one board, and what it knows about it.
pub struct StatemachinedDevice {
    pub target: String,
    pub baud: u32,
    pub timeout: Duration,
    /// What somebody wrote in the config file.
    pub line_map: LineMap,
    /// What board this rig is supposed to have, or `""` for "do not check".
    pub expected_board: String,
    /// Fixed for the life of the supervisor when given, so a whole session —
    /// including one interrupted by a reconnect — can be replayed. Drawn fresh
    /// per connection when it is not.
    pub configured_session_seed: Option<String>,

    session: Option<RequestResponseSession<Discard>>,
    pub hello_ack: Option<Value>,
    /// What this board can hold, from its greeting. `None` until one greets.
    pub capabilities: Option<DeviceCapabilities>,
    pub session_seed: Option<String>,
    /// What the board says its pins are called, or what this daemon assumed
    /// when the board could not say. Read once per connection, because it
    /// cannot change without a reflash — and a reflash is a reconnect.
    pub pin_map: DevicePinMap,
    /// The configured map with every index resolved and checked against the
    /// board. **This is what is pushed and what graphs compile against.**
    pub resolved_line_map: LineMap,
    pub clock: DeviceClockCorrelation,
    /// Every reconnection, counted. A link that flaps should be visible to
    /// whoever is debugging the rig rather than inferred from trials that did
    /// not happen.
    pub connection_count: u64,
    /// The set the board said `set_ok` to, and what was compiled into it.
    ///
    /// **Believed only after `set_ok`.** From `set_begin` until then the board
    /// holds no graph at all (PROTOCOL.md 3.2), so this is cleared before an
    /// upload and set after one — never left naming a set the board would
    /// contradict.
    pub committed_graph_set: Option<CompiledGraphSet>,
    /// The documents behind it, kept so a board that comes back from a reset
    /// without its set can be given the same one again.
    graphs_of_the_committed_set: Vec<GraphDefinition>,
}

impl StatemachinedDevice {
    pub fn new(target: impl Into<String>, line_map: LineMap) -> Self {
        Self {
            target: target.into(),
            baud: super::serial_link::DEFAULT_BAUD,
            timeout: super::serial_link::DEFAULT_TIMEOUT,
            resolved_line_map: line_map.clone(),
            line_map,
            expected_board: String::new(),
            configured_session_seed: None,
            session: None,
            hello_ack: None,
            capabilities: None,
            session_seed: None,
            pin_map: DevicePinMap::default(),
            clock: DeviceClockCorrelation::new(),
            connection_count: 0,
            committed_graph_set: None,
            graphs_of_the_committed_set: Vec::new(),
        }
    }

    pub fn is_connected(&self) -> bool {
        self.session.is_some()
    }

    fn require_session(&mut self) -> Result<&mut RequestResponseSession<Discard>, DeviceProblem> {
        let target = self.target.clone();
        self.session
            .as_mut()
            .ok_or_else(|| DeviceProblem::NotConnected(format!("no link to {target} is open")))
    }

    /// Open the port and say nothing.
    ///
    /// For a board that is running on its own: **greeting it would take the
    /// rig** — cancelling the run in flight and stopping it driving itself —
    /// and there are times when what is wanted is to watch, not to take over.
    ///
    /// Nothing else works on this connection. Every command but `hello` is
    /// refused by a device nobody has greeted, which is the rule that makes
    /// this safe rather than a way to half-connect.
    pub fn connect_and_watch(&mut self) -> Result<(), DeviceProblem> {
        self.disconnect();
        let mut link = SerialLink::open(&self.target, self.baud, self.timeout)?;
        link.reset_input();
        self.session = Some(RequestResponseSession::new(link, Discard));
        self.clock.forget_everything_observed();
        self.connection_count += 1;
        Ok(())
    }

    /// Open the port, say hello, and push this rig's wiring.
    ///
    /// **In that order and not another.**
    pub fn connect_and_greet(&mut self) -> Result<Value, DeviceProblem> {
        self.disconnect();
        let mut link = SerialLink::open(&self.target, self.baud, self.timeout)?;
        link.reset_input();
        let mut session = RequestResponseSession::new(link, Discard);

        let seed = self
            .configured_session_seed
            .clone()
            .unwrap_or_else(random_seed);
        let hello_ack = session.hello(Some(&seed), self.timeout)?;

        self.session = Some(session);
        self.session_seed = Some(seed);
        self.hello_ack = Some(hello_ack.clone());
        self.capabilities = Some(DeviceCapabilities::from_hello_ack(&hello_ack));
        // A reset device restarts its clock from zero, so an offset measured
        // before the reconnect would be wrong by however long the board was
        // away -- and wrong plausibly, which is the worst kind.
        self.clock.forget_everything_observed();
        self.connection_count += 1;

        // Before the pin map, because the pin map is the thing that would
        // otherwise make the wrong board look right.
        if let Err(problem) = self.refuse_a_board_this_rig_is_not_wired_for(&hello_ack) {
            self.disconnect();
            return Err(problem);
        }

        // Before the wiring, because the wiring is a set of masks over line
        // numbers and this is what says which number is which pin. A line map
        // that does not match the board is refused here -- with the link closed
        // again -- rather than pushed: masks built from a wrong index are a
        // valve driven from a lever's line, and nothing downstream would say so.
        self.pin_map = self.read_pin_map()?;
        match self.line_map.resolved_against(&self.pin_map) {
            Ok(resolved) => self.resolved_line_map = resolved,
            Err(problem) => {
                self.disconnect();
                return Err(DeviceProblem::LineMap(problem.to_string()));
            }
        }

        self.push_wiring()?;
        Ok(hello_ack)
    }

    /// Stop here if this is not the board the rig config names.
    ///
    /// A line map is checked against the pins the board reports, which catches
    /// a pin that does not exist — and **misses the case that matters most,
    /// because pin names repeat across boards.** A Teensy 4.1 has an `A0` and
    /// so does an R4 Minima, they are not the same hole, and a map written for
    /// one resolves perfectly against the other. Nothing downstream would
    /// notice: the indices are valid, the wiring pushes, the graphs upload, and
    /// the first sign is an animal being rewarded by a lamp.
    fn refuse_a_board_this_rig_is_not_wired_for(
        &self,
        hello_ack: &Value,
    ) -> Result<(), DeviceProblem> {
        if self.expected_board.is_empty() {
            return Ok(());
        }
        let board = hello_ack
            .get("board")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if board == self.expected_board {
            return Ok(());
        }
        Err(DeviceProblem::WrongBoard(format!(
            "this rig is configured for a '{}' board and the device on {} says it is a '{}'. \
             Its pin names may look right and mean different holes, so nothing is pushed to \
             it. Change expected_board in the rig config, or plug in the board this rig is \
             wired for",
            self.expected_board,
            self.target,
            if board.is_empty() { "(unnamed)" } else { board }
        )))
    }

    pub fn disconnect(&mut self) {
        self.session = None;
    }

    /// Come back after a link loss, and put the device back as it was.
    ///
    /// The committed set survives a reconnect — that is what `hello_ack`'s
    /// `has_set` and `set_version` are for, and why a bridge restarting does
    /// not cost a re-upload. So this re-uploads only when the board came back
    /// without the set this supervisor believes in.
    pub fn reconnect_and_restore(&mut self) -> Result<Value, UploadProblem> {
        let hello_ack = self.connect_and_greet()?;
        let Some(committed) = &self.committed_graph_set else {
            return Ok(hello_ack);
        };
        let holds_it = hello_ack.get("has_set").and_then(Value::as_bool) == Some(true)
            && hello_ack.get("set_version").and_then(Value::as_i64) == Some(committed.set_version);
        if !holds_it {
            let set_version = committed.set_version;
            let graphs = self.graphs_of_the_committed_set.clone();
            self.upload_graph_set(&graphs, set_version)?;
        }
        Ok(hello_ack)
    }

    /// Compile the session's graphs and put the whole set on the device.
    ///
    /// The slow call, and the one where a session is allowed to fail: a graph
    /// too big for this board is refused here, minutes before an animal is in
    /// the booth, rather than at trial 40.
    pub fn upload_graph_set(
        &mut self,
        graphs: &[GraphDefinition],
        set_version: i64,
    ) -> Result<CompiledGraphSet, UploadProblem> {
        self.require_session()?;
        let Some(capabilities) = &self.capabilities else {
            return Err(DeviceProblem::NotConnected(
                "the device has not been greeted, so its caps are unknown".into(),
            )
            .into());
        };
        let compiled =
            compile_graph_set_for_device(graphs, &self.resolved_line_map, capabilities, set_version)
                .map_err(UploadProblem::Compile)?;
        // Not committed on this side until the device says set_ok. From
        // set_begin until then the board holds no graph at all, so believing
        // otherwise here would be believing something the board would
        // contradict.
        self.committed_graph_set = None;
        let timeout = self.timeout;
        send_compiled_upload_messages(self.require_session()?, &compiled.upload_messages, timeout)?;
        self.committed_graph_set = Some(compiled.clone());
        self.graphs_of_the_committed_set = graphs.to_vec();
        Ok(compiled)
    }

    /// Ask the board what its pins are called. **Never fatal.**
    ///
    /// Two requests, one per direction, because the protocol answers one at a
    /// time — both directions do not fit one line on a board with many lines,
    /// and a reply carrying half a map would be worse than none.
    ///
    /// A board that refuses — `no_pin_map`, or `unknown_type` from firmware
    /// flashed before the command existed — is **not an error**. It is the
    /// common case in a rack that has not been reflashed yet, and the daemon
    /// falls back to its own table *and says that it did*.
    pub fn read_pin_map(&mut self) -> Result<DevicePinMap, DeviceProblem> {
        let timeout = self.timeout;
        let session = self.require_session()?;
        // Asked one direction at a time, and the second only if the first
        // arrived: a board that refuses `pins` refuses both, and asking again
        // to learn the same thing costs a round trip on every connection.
        let inputs = match session.request(MsgType::Pins, timeout, &[("dir", Value::from("in"))]) {
            Ok(reply) => reply,
            // Not an error: `no_pin_map`, or `unknown_type` from firmware older
            // than the command.
            Err(RequestProblem::Refused(_)) => return Ok(self.pin_map_this_daemon_assumes()),
            Err(problem) => return Err(problem.into()),
        };
        let session = self.require_session()?;
        let outputs = match session.request(MsgType::Pins, timeout, &[("dir", Value::from("out"))])
        {
            Ok(reply) => reply,
            // Half a map is worse than none, so a board that answers for one
            // direction and not the other falls back whole.
            Err(RequestProblem::Refused(_)) => return Ok(self.pin_map_this_daemon_assumes()),
            Err(problem) => return Err(problem.into()),
        };
        Ok(DevicePinMap {
            input_pin_labels: labels_in(&inputs),
            output_pin_labels: labels_in(&outputs),
            source: PinLabelSource::Device,
        })
    }

    /// The host's own table, for a board that cannot answer for itself.
    ///
    /// Marked `Assumed` all the way up to the UI. It is a hand-copied pin map,
    /// which is the thing the `pins` command exists to stop being the only
    /// option — so it is used, and **it is never presented as the board's
    /// word**.
    fn pin_map_this_daemon_assumes(&self) -> DevicePinMap {
        let board = self
            .hello_ack
            .as_ref()
            .and_then(|ack| ack.get("board"))
            .and_then(Value::as_str)
            .unwrap_or_default();
        let inputs = board_pin_labels::pins_of(board, Direction::In);
        let outputs = board_pin_labels::pins_of(board, Direction::Out);
        if inputs.is_empty() && outputs.is_empty() {
            return DevicePinMap {
                source: PinLabelSource::Unknown,
                ..Default::default()
            };
        }
        DevicePinMap {
            input_pin_labels: inputs.iter().map(|pin| (*pin).to_string()).collect(),
            output_pin_labels: outputs.iter().map(|pin| (*pin).to_string()).collect(),
            source: PinLabelSource::Assumed,
        }
    }

    /// Tell the board what it is wired to.
    pub fn push_wiring(&mut self) -> Result<Value, DeviceProblem> {
        let wiring = self
            .resolved_line_map
            .wiring_message_fields()
            .map_err(|problem| DeviceProblem::LineMap(problem.to_string()))?;
        let timeout = self.timeout;
        let fields = [
            ("invert", Value::from(wiring.invert)),
            ("enable", Value::from(wiring.enable)),
            ("safe", Value::from(wiring.safe)),
            ("debounce_ms", Value::from(wiring.debounce_ms)),
        ];
        Ok(self.require_session()?.request(MsgType::Wiring, timeout, &fields)?)
    }

    /// What the board says about itself right now.
    pub fn read_state_report(&mut self) -> Result<Value, DeviceProblem> {
        let timeout = self.timeout;
        Ok(self.require_session()?.state(timeout)?)
    }

    /// One `ping`, folded into the clock estimate.
    ///
    /// The supervisor sends these for the link-loss watchdog anyway, so the
    /// offset is refreshed for free — which is why no drift is modelled.
    pub fn send_heartbeat_ping(&mut self) -> Result<Value, DeviceProblem> {
        let timeout = self.timeout;
        let before = unix_seconds_now();
        let pong = self.require_session()?.ping(timeout)?;
        let after = unix_seconds_now();
        if let Some(device_microseconds) = pong.get("t_us").and_then(Value::as_i64) {
            // A negative round trip means the host clock stepped mid-request,
            // which is not the device's fault and is not worth failing a
            // heartbeat over -- the estimate simply keeps the one it had.
            let _ = self
                .clock
                .observe_ping_round_trip(before, device_microseconds as i128, after);
        }
        Ok(pong)
    }
}

fn labels_in(reply: &Value) -> Vec<String> {
    reply
        .get("pins")
        .and_then(Value::as_array)
        .map(|pins| {
            pins.iter()
                .map(|pin| match pin.as_str() {
                    Some(text) => text.to_string(),
                    None => pin.to_string(),
                })
                .collect()
        })
        .unwrap_or_default()
}

fn unix_seconds_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("a clock after 1970")
        .as_secs_f64()
}
