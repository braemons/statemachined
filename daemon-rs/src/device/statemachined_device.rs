// SPDX-License-Identifier: AGPL-3.0-or-later
//! One MCU, for as long as the daemon is running.
//!
//! The connection lifecycle — opening a link, greeting the board, checking it
//! is the board this rig is wired for, resolving the line map against its pins,
//! and pushing the wiring — the graph-set upload, one trial at a time (arming
//! it, reading the visits it streams, naming the result it sends back), and the
//! board on its own: autorun and its saved settings. `set_enabled_timers` is
//! not ported: no rpc calls it.
//!
//! The ordering in `connect_and_greet` is the part to keep: **the greeting is
//! what takes the rig** from a board that was arming its own trials, and the
//! wiring is what makes the board's fail-safe correct for *this* box — so it
//! goes before any graph and long before any trial.

use std::time::Duration;

use serde_json::Value;

use super::board_pin_labels;
use super::device_clock_correlation::DeviceClockCorrelation;
use super::device_clock_correlation::HostTimeEstimate;
use super::device_pin_map::{DevicePinMap, Direction, PinLabelSource};
use super::graph_set_upload::send_compiled_upload_messages;
use super::message_vocabulary::field;
use super::message_vocabulary::MsgType;
use super::request_response_session::{random_seed, RequestProblem, RequestResponseSession, Sink};
use super::serial_link::SerialLink;
use super::trial_result_reassembly::{ReassembledTrialResult, TrialResultCollector};
use crate::graph_set_compiler::{
    compile_graph_set_for_device, CompileError, CompiledGraphSet, DeviceCapabilities,
};
use crate::model::graph_definition::GraphDefinition;
use crate::model::line_map::LineMap;
use crate::model::trial_record::{decode_state_visit_row, StateVisitRecord, TrialResultRecord};

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

/// What the session set aside while a request was in flight, kept for the
/// device to route: visits, result chunks, a trial the line started.
///
/// Python hands these to a callback inside the request. Here they wait in the
/// sink until the request returns, and are routed then — before the device
/// changes anything on the strength of the reply, which is the same order.
#[derive(Default)]
pub struct Collected {
    pending: std::collections::VecDeque<(Value, Vec<u8>)>,
}

impl Sink for Collected {
    fn unsolicited(&mut self, message: &Value, payload: &[u8]) {
        self.pending.push_back((message.clone(), payload.to_vec()));
    }
    fn junk(&mut self, _text: &str, _why: &str) {}
}

/// One `visit` off the wire, named, timestamped and placed in host time.
///
/// The three timebases are kept side by side on purpose: `raw` is what the
/// device said and is the evidence; `unwrapped` is that made monotonic for
/// this connection; `host_time` is an estimate, labelled as one, and `None`
/// until a `ping` has been answered.
#[derive(Debug, Clone, PartialEq)]
pub struct ObservedStateVisit {
    pub trial_id: i64,
    pub sequence_number: i64,
    pub visit: StateVisitRecord,
    pub unwrapped_device_microseconds: i128,
    pub host_time: Option<HostTimeEstimate>,
}

/// What the device said that nobody asked for, routed.
#[derive(Debug, Clone, PartialEq)]
pub enum DeviceEvent {
    Visit(ObservedStateVisit),
    Result(TrialResultRecord),
    /// Anything else: an event, a log, a late reply — or a visit or result
    /// this daemon cannot name, because it did not arm the run.
    Unsolicited(Value),
}

/// Why a trial command was not sent: no board, no set, or the board said no.
#[derive(Debug)]
pub enum TrialProblem {
    Device(DeviceProblem),
    NoGraphSetCommitted(String),
    NotInSet(CompileError),
}

impl std::fmt::Display for TrialProblem {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Device(problem) => write!(f, "{problem}"),
            Self::NoGraphSetCommitted(sentence) => f.write_str(sentence),
            Self::NotInSet(problem) => write!(f, "{problem}"),
        }
    }
}

impl<T: Into<DeviceProblem>> From<T> for TrialProblem {
    fn from(problem: T) -> Self {
        Self::Device(problem.into())
    }
}

/// The arguments of `configure`, by name.
#[derive(Debug, Clone, Default)]
pub struct TrialConfiguration {
    pub trial_id: i64,
    pub graph_name: String,
    pub cap_milliseconds: i64,
    pub start_source: String,
    pub start_line: Option<i64>,
    pub timers: Option<i64>,
    /// Already translated to pool indices and the wire's `a`/`b`/`c`.
    pub distribution_patches: Vec<Value>,
}

/// The arguments of `autorun`, by name. `None` leaves the board's own value.
#[derive(Debug, Clone, Default)]
pub struct AutorunSetting {
    pub enabled: bool,
    pub graph_name: Option<String>,
    pub cap_milliseconds: i64,
    pub seed: Option<i64>,
    pub first_trial_id: Option<i64>,
    pub start_now: bool,
}

/// A session seed as a rig's configuration writes it -- one to sixteen hex
/// digits -- as the 64-bit number the link carries. The board used to parse
/// this itself and refuse what was not hex; now the daemon does, before the
/// greeting rather than in reply to it.
pub fn parse_session_seed(text: &str) -> Result<u64, String> {
    if text.is_empty() || text.len() > 16 || !text.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(format!(
            "session seed {text:?} is not one to sixteen hex digits"
        ));
    }
    u64::from_str_radix(text, 16).map_err(|problem| problem.to_string())
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

    session: Option<RequestResponseSession<Collected>>,
    /// What has been routed and not yet taken by the daemon.
    events: Vec<DeviceEvent>,
    result_collector: TrialResultCollector,
    /// The last unsolicited `started`: a trial the *line* began.
    pub last_line_start: Option<Value>,
    pub armed_trial_id: Option<i64>,
    pub armed_graph_name: Option<String>,
    /// The graph a self-driving board was pointed at, which names the results
    /// of runs this daemon did not arm.
    pub autorun_graph_name: Option<String>,
    /// Every line crossing the wire, for the serial monitor.
    pub line_monitor: Option<std::sync::Arc<super::device_line_monitor::DeviceLineMonitor>>,
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
            events: Vec::new(),
            result_collector: TrialResultCollector::new(),
            last_line_start: None,
            armed_trial_id: None,
            armed_graph_name: None,
            autorun_graph_name: None,
            line_monitor: None,
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

    /// A link to the target, with the monitor watching every line on it.
    ///
    /// Handed to each link rather than held here, because the transport is
    /// the only place that sees a line before anything has decided whether it
    /// means anything.
    fn open_link(&self) -> Result<SerialLink, DeviceProblem> {
        let mut link = SerialLink::open(&self.target, self.baud, self.timeout)?;
        if let Some(monitor) = &self.line_monitor {
            let monitor = monitor.clone();
            link.observe(move |direction, line| monitor.record(direction, line));
        }
        Ok(link)
    }

    pub fn is_connected(&self) -> bool {
        self.session.is_some()
    }

    fn require_session(&mut self) -> Result<&mut RequestResponseSession<Collected>, DeviceProblem> {
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
        let mut link = self.open_link()?;
        link.reset_input();
        self.session = Some(RequestResponseSession::new(link, Collected::default()));
        self.clock.forget_everything_observed();
        self.connection_count += 1;
        Ok(())
    }

    /// Open the port, say hello, and push this rig's wiring.
    ///
    /// **In that order and not another.**
    pub fn connect_and_greet(&mut self) -> Result<Value, DeviceProblem> {
        self.disconnect();
        let mut link = self.open_link()?;
        link.reset_input();
        let mut session = RequestResponseSession::new(link, Collected::default());

        let seed = match &self.configured_session_seed {
            Some(text) => parse_session_seed(text).map_err(RequestProblem::Unsendable)?,
            None => random_seed(),
        };
        let hello_ack = session.hello(Some(seed), self.timeout)?;

        self.session = Some(session);
        self.session_seed = Some(format!("{seed:016X}"));
        self.hello_ack = Some(hello_ack.clone());
        self.capabilities = Some(DeviceCapabilities::from_hello_ack(&hello_ack));
        // A reset device restarts its clock from zero, so an offset measured
        // before the reconnect would be wrong by however long the board was
        // away -- and wrong plausibly, which is the worst kind.
        self.clock.forget_everything_observed();
        self.connection_count += 1;
        self.armed_trial_id = None;
        self.armed_graph_name = None;
        // Whatever the previous session was armed on went away with it.
        self.last_line_start = None;
        self.route_what_arrived();

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
        let compiled = compile_graph_set_for_device(
            graphs,
            &self.resolved_line_map,
            capabilities,
            set_version,
        )
        .map_err(UploadProblem::Compile)?;
        // Not committed on this side until the device says set_ok. From
        // set_begin until then the board holds no graph at all, so believing
        // otherwise here would be believing something the board would
        // contradict.
        self.committed_graph_set = None;
        let timeout = self.timeout;
        let sent = send_compiled_upload_messages(
            self.require_session()?,
            &compiled.upload_messages,
            timeout,
        );
        self.route_what_arrived();
        sent?;
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
            // Empty would leave the board's table alone -- protobuf cannot
            // tell an empty list from an absent one -- where this means "no
            // debounce anywhere". One zero says that.
            (
                "debounce_ms",
                Value::from(if wiring.debounce_ms.is_empty() {
                    vec![0]
                } else {
                    wiring.debounce_ms
                }),
            ),
        ];
        let reply = self
            .require_session()?
            .request(MsgType::Wiring, timeout, &fields);
        self.route_what_arrived();
        Ok(reply?)
    }

    /// What the board says about itself right now.
    pub fn read_state_report(&mut self) -> Result<Value, DeviceProblem> {
        let timeout = self.timeout;
        let report = self.require_session()?.state(timeout);
        self.route_what_arrived();
        Ok(report?)
    }

    /// One `ping`, folded into the clock estimate.
    ///
    /// The supervisor sends these for the link-loss watchdog anyway, so the
    /// offset is refreshed for free — which is why no drift is modelled.
    pub fn send_heartbeat_ping(&mut self) -> Result<Value, DeviceProblem> {
        let timeout = self.timeout;
        let before = unix_seconds_now();
        let pong = self.require_session()?.ping(timeout);
        let after = unix_seconds_now();
        self.route_what_arrived();
        let pong = pong?;
        // `us`, the device clock raw; `up_us` counts from boot and is not the
        // clock a visit is stamped with (protocol.md 4.5).
        if let Some(device_microseconds) = pong.get("us").and_then(Value::as_i64) {
            // A negative round trip means the host clock stepped mid-request,
            // which is not the device's fault and is not worth failing a
            // heartbeat over -- the estimate simply keeps the one it had.
            let _ = self
                .clock
                .observe_ping_round_trip(before, device_microseconds as i128, after);
        }
        Ok(pong)
    }

    // -- a trial, once ----------------------------------------------------------

    fn require_committed_graph_set(&self) -> Result<&CompiledGraphSet, TrialProblem> {
        self.committed_graph_set.as_ref().ok_or_else(|| {
            TrialProblem::NoGraphSetCommitted(
                "no graph set has been uploaded, so no trial can name a graph".into(),
            )
        })
    }

    /// One command, and whatever arrived while it was in flight routed before
    /// anything is done with the reply.
    fn ask(&mut self, msg_type: MsgType, fields: &[(&str, Value)]) -> Result<Value, DeviceProblem> {
        let timeout = self.timeout;
        let reply = self.require_session()?.request(msg_type, timeout, fields);
        self.route_what_arrived();
        Ok(reply?)
    }

    /// Arm the device for one trial of one graph.
    ///
    /// `graph_name`, never an index: the daemon built the set, so the daemon
    /// knows which slot the name is in — and an index on the caller's side
    /// would be a cache to get wrong across a re-upload. `start_line` is the
    /// input whose **rising edge** starts the trial, required when the start
    /// source admits a line.
    pub fn configure_trial(&mut self, trial: &TrialConfiguration) -> Result<Value, TrialProblem> {
        self.require_session()?;
        let compiled = self.require_committed_graph_set()?;
        let graph_index = compiled
            .slot_for_graph_name(&trial.graph_name)
            .map_err(TrialProblem::NotInSet)?;
        let mut fields: Vec<(&str, Value)> = vec![
            ("trial_id", Value::from(trial.trial_id)),
            ("set_version", Value::from(compiled.set_version)),
            ("graph_index", Value::from(graph_index)),
            ("start", Value::from(trial.start_source.as_str())),
        ];
        if let Some(start_line) = trial.start_line {
            fields.push(("start_line", Value::from(start_line)));
        }
        if let Some(timers) = trial.timers {
            fields.push(("timers", Value::from(timers)));
        }
        if trial.cap_milliseconds != 0 {
            fields.push(("cap_ms", Value::from(trial.cap_milliseconds)));
        }
        if !trial.distribution_patches.is_empty() {
            fields.push(("patch", Value::from(trial.distribution_patches.clone())));
        }
        let armed = self.ask(MsgType::Configure, &fields)?;
        self.armed_trial_id = Some(trial.trial_id);
        self.armed_graph_name = Some(trial.graph_name.clone());
        Ok(armed)
    }

    pub fn start_trial(&mut self, trial_id: i64) -> Result<Value, DeviceProblem> {
        self.ask(MsgType::Start, &[("trial_id", Value::from(trial_id))])
    }

    /// Ask for a cancel, and report whatever actually happened.
    ///
    /// A cancel that races a terminal state comes back with the **real**
    /// outcome, passed through unchanged: the alternative is a record claiming
    /// a trial was cancelled when the animal had already responded.
    pub fn cancel_trial(&mut self, trial_id: i64) -> Result<Value, DeviceProblem> {
        self.ask(
            MsgType::Cancel,
            &[
                ("trial_id", Value::from(trial_id)),
                ("reason", Value::from("host")),
            ],
        )
    }

    // -- the board on its own ----------------------------------------------------

    /// Hand the board the job of arming its own trials, or take it back.
    ///
    /// The one thing this daemon does that makes itself optional
    /// (protocol.md 3.7). `graph_name`, not an index, as for a trial; autorun
    /// cannot switch paradigms afterwards. `start_now` false records that the
    /// board should drive itself without starting it — how a rig is set up,
    /// because `save` is refused on a board that is running.
    pub fn set_autorun(&mut self, autorun: &AutorunSetting) -> Result<Value, TrialProblem> {
        self.require_session()?;
        let mut fields: Vec<(&str, Value)> = vec![("enabled", Value::from(autorun.enabled))];
        if let Some(graph_name) = &autorun.graph_name {
            let slot = self
                .require_committed_graph_set()?
                .slot_for_graph_name(graph_name)
                .map_err(TrialProblem::NotInSet)?;
            fields.push(("graph_index", Value::from(slot)));
        }
        if autorun.cap_milliseconds != 0 {
            fields.push(("cap_ms", Value::from(autorun.cap_milliseconds)));
        }
        if let Some(seed) = autorun.seed {
            // A negative one is refused by the codec, as the board refused the
            // signed hex it used to be sent as.
            fields.push(("seed", Value::from(seed)));
        }
        if let Some(first_trial_id) = autorun.first_trial_id {
            fields.push(("first_trial_id", Value::from(first_trial_id)));
        }
        if !autorun.start_now {
            fields.push(("start_now", Value::from(false)));
        }
        let reply = self.ask(MsgType::Autorun, &fields)?;
        // Remembered so the results of runs this daemon did not arm can still
        // be named: the device reports indices, and only the graph has names.
        if autorun.enabled {
            if let Some(graph_name) = &autorun.graph_name {
                self.autorun_graph_name = Some(graph_name.clone());
            }
        } else {
            self.autorun_graph_name = None;
        }
        Ok(reply)
    }

    /// What the board would do on its own, asked rather than remembered: the
    /// settings outlive the session that set them.
    pub fn read_autorun(&mut self) -> Result<Value, DeviceProblem> {
        self.ask(MsgType::Autorun, &[])
    }

    /// Write the board's wiring, graph set and autorun settings to its own
    /// storage (protocol.md 3.8). Slow — it erases and programs flash — so
    /// its timeout is at least five seconds.
    pub fn save_settings(&mut self) -> Result<Value, DeviceProblem> {
        let timeout = self.timeout.max(Duration::from_secs(5));
        let reply = self.require_session()?.request(MsgType::Save, timeout, &[]);
        self.route_what_arrived();
        Ok(reply?)
    }

    // -- reading the link --------------------------------------------------------

    /// Read whatever the device has said, for a bounded moment.
    ///
    /// The daemon's read path: returns after `budget` whatever happened, so the
    /// thread calling it can give the link back. The budget is the read's
    /// timeout, not a loop condition around a blocking read — this is called
    /// holding the device lock, and a read that waited for the command timeout
    /// would make every request queue behind an idle link.
    pub fn pump_incoming_lines(&mut self, budget: Duration) -> Result<usize, DeviceProblem> {
        let deadline = std::time::Instant::now() + budget;
        let mut lines_read = 0;
        loop {
            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() {
                break;
            }
            let session = self.require_session()?;
            let Some(frame) = session.link.read_frame(Some(remaining))? else {
                break;
            };
            lines_read += 1;
            if let Some(message) = session.receive(frame) {
                // A reply to a command nobody is waiting for: one that timed
                // out and whose answer arrived late. Worth seeing.
                self.route_what_arrived();
                self.events.push(DeviceEvent::Unsolicited(message));
            } else {
                self.route_what_arrived();
            }
        }
        Ok(lines_read)
    }

    /// Everything routed since the last call, in the order it arrived.
    pub fn take_events(&mut self) -> Vec<DeviceEvent> {
        self.route_what_arrived();
        std::mem::take(&mut self.events)
    }

    /// Route what the session set aside.
    ///
    /// `visit` is decoded here because this is the only place that holds both
    /// halves: the compiled graph that gives an index a name, and the clock
    /// that gives a device microsecond a host time. Result chunks go to the
    /// collector. Everything else is handed on untouched.
    fn route_what_arrived(&mut self) {
        loop {
            let Some((message, payload)) = self
                .session
                .as_mut()
                .and_then(|session| session.sink.pending.pop_front())
            else {
                return;
            };
            let kind = message
                .get(field::MSG_TYPE)
                .and_then(Value::as_str)
                .and_then(MsgType::named);
            match kind {
                Some(MsgType::Started) => {
                    // A trial the line began: remembered, so a caller that
                    // armed on a line has something to wait for.
                    self.last_line_start = Some(message.clone());
                    self.events.push(DeviceEvent::Unsolicited(message));
                }
                Some(MsgType::ResultBegin | MsgType::ResultPath | MsgType::ResultEnd) => {
                    if !payload.is_empty() {
                        self.collect_result_chunk(&payload, &message);
                    }
                }
                Some(MsgType::Visit) => {
                    if let Some(observed) = self.decode_visit_message(&message) {
                        self.events.push(DeviceEvent::Visit(observed));
                    }
                }
                _ => self.events.push(DeviceEvent::Unsolicited(message)),
            }
        }
    }

    fn collect_result_chunk(&mut self, payload: &[u8], message: &Value) {
        let reassembled = match self.result_collector.feed(payload, message) {
            Ok(Some(reassembled)) => reassembled,
            Ok(None) => return,
            Err(sentence) => {
                self.events.push(DeviceEvent::Unsolicited(
                    serde_json::json!({"msg_type": "error", "message": sentence}),
                ));
                return;
            }
        };
        match self.name_a_reassembled_result(&reassembled) {
            Some(Ok(named)) => self.events.push(DeviceEvent::Result(named)),
            // A result from a run this daemon did not configure: real, and
            // unreadable without the graph, so it goes on as it came rather
            // than decoded into a guess.
            None => self
                .events
                .push(DeviceEvent::Unsolicited(reassembled.begin)),
            Some(Err(sentence)) => self.events.push(DeviceEvent::Unsolicited(
                serde_json::json!({"msg_type": "error", "message": sentence}),
            )),
        }
    }

    /// Whose graph the run that just ended was: the trial this daemon armed,
    /// or the graph a self-driving board was pointed at.
    fn graph_name_for_reporting(&self) -> Option<&str> {
        self.armed_graph_name
            .as_deref()
            .or(self.autorun_graph_name.as_deref())
    }

    /// A result's indices, turned into the names of the graph that ran it.
    /// `None` when this daemon cannot know which graph that was.
    fn name_a_reassembled_result(
        &self,
        reassembled: &ReassembledTrialResult,
    ) -> Option<Result<TrialResultRecord, String>> {
        let compiled = self.committed_graph_set.as_ref()?;
        let graph_name = self.graph_name_for_reporting()?;
        let graph = match compiled.graph_named(graph_name) {
            Ok(graph) => graph,
            Err(problem) => return Some(Err(problem.to_string())),
        };
        let begin = &reassembled.begin;
        let number =
            |key: &str, default: i64| begin.get(key).and_then(Value::as_i64).unwrap_or(default);
        let visits = reassembled
            .rows
            .iter()
            .map(|row| {
                decode_state_visit_row(
                    row,
                    &graph.state_names_by_index,
                    &graph.transition_target_names_by_state_index,
                )
            })
            .collect::<Result<Vec<_>, _>>();
        Some(visits.map(|visits| {
            TrialResultRecord {
                trial_id: number("trial_id", 0),
                outcome: number("outcome", 0) as i32,
                cancel_reason: number("cancel_reason", 0) as i32,
                total_duration_microseconds: number("total_us", 0),
                path_was_truncated: begin
                    .get("truncated")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                first_visit_sequence_number: number("first_seq", 0),
                total_visit_count: number("total_visits", reassembled.rows.len() as i64),
                visits,
            }
        }))
    }

    fn decode_visit_message(&mut self, message: &Value) -> Option<ObservedStateVisit> {
        let named = (|| {
            let compiled = self.committed_graph_set.as_ref()?;
            let graph = compiled
                .graph_named(self.graph_name_for_reporting()?)
                .ok()?;
            let row = message.get("v")?;
            Some(decode_state_visit_row(
                row,
                &graph.state_names_by_index,
                &graph.transition_target_names_by_state_index,
            ))
        })();
        let visit = match named {
            Some(Ok(visit)) => visit,
            Some(Err(sentence)) => {
                self.events.push(DeviceEvent::Unsolicited(
                    serde_json::json!({"msg_type": "error", "message": sentence}),
                ));
                return None;
            }
            None => {
                // A visit from a run this daemon did not configure. Real, and
                // unreadable without a graph: handed on, not decoded into a
                // guess.
                self.events.push(DeviceEvent::Unsolicited(message.clone()));
                return None;
            }
        };
        // Unwrapped here, once, in arrival order: the stream is the only
        // place that sees every device timestamp exactly once and in sequence,
        // which is what makes unwrapping correct at all.
        let unwrapped = self
            .clock
            .unwrap_device_microseconds(visit.entered_device_microseconds as i128);
        Some(ObservedStateVisit {
            trial_id: message.get("trial_id").and_then(Value::as_i64).unwrap_or(0),
            sequence_number: message.get("seq").and_then(Value::as_i64).unwrap_or(0),
            host_time: self
                .clock
                .host_time_for_unwrapped_device_microseconds(unwrapped),
            unwrapped_device_microseconds: unwrapped,
            visit,
        })
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
