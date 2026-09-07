# SPDX-License-Identifier: LGPL-3.0-or-later
"""The one object that owns a device: the port, the session, and what it holds.

Everything below this file is stateless about the *rig*. `SerialLink` knows a
URL, `RequestResponseSession` knows one command is in flight, the compiler knows
names. None of them knows that this board was greeted with this seed, is holding
this graph set, and was armed for trial 193 -- and something has to, because a
daemon outlives a command and a device does not answer questions about its own
history.

What that ownership buys, in the order the problems arrive:

  * **The seed.** One per session, held here, mixed with the trial id on the
    device so replaying trial 412 alone draws trial 412's numbers. A daemon that
    let each command carry its own seed would make a session unreproducible and
    nobody would notice until the analysis.

  * **The wiring first.** dev/DAEMON.md 3.4: a board's compile-time safe levels
    are what hold the fail-safe hole shut, and the daemon's job on connecting is
    to replace them with this rig's before anything else happens -- before a
    graph, certainly before a trial.

  * **The reconnect.** A USB port closes when a bridge restarts, and the
    committed set survives it (`hello_ack` says so). So a reconnect re-greets,
    checks whether the set is still there, and re-uploads only if it is not.
    The seed changes, because it is per session and a new session is what a
    `hello` opens.

  * **The clock.** Device microseconds mean nothing off the device, so a `ping`
    -- which the link-loss watchdog wants anyway -- doubles as the correlation
    observation. See device_clock_correlation.py.

What this is NOT is a decision authority. It reports what the device measured
and sets no veto field; every outcome it returns is the device's own, including
a cancel that lost its race and came back `HIT`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..board_pin_labels import PIN_MAPS
from ..graph_set_compiler import (
    CompiledGraphSet,
    DeviceCapabilities,
    compile_graph_set_for_device,
)
from ..model.graph_definition import GraphDefinition
from ..model.line_map import LineMap
from ..model.trial_outcome import TrialCancelReason, TrialOutcome
from ..model.trial_record import (
    StateVisitRecord,
    TrialResultRecord,
    decode_state_visit_row,
)
from .device_clock_correlation import DeviceClockCorrelation, HostTimeEstimate
from .device_pin_map import DevicePinMap
from .graph_set_upload import send_compiled_upload_messages
from .message_framing import DeviceRefusedTheCommand
from .message_vocabulary import Field, MsgType
from .request_response_session import (
    PROTOCOL_VERSION,
    RequestResponseSession,
    random_seed,
)
from .serial_link import DEFAULT_BAUD, DEFAULT_TARGET, DEFAULT_TIMEOUT, SerialLink
from .trial_result_reassembly import (
    ReassembledTrialResult,
    TrialResultCollector,
    read_trial_result,
)


class DeviceNotConnected(RuntimeError):
    """A command was asked for while no link was open."""


class NoGraphSetCommitted(RuntimeError):
    """A trial was asked for before the session's graphs were uploaded."""


@dataclass(frozen=True)
class ObservedStateVisit:
    """One `visit` off the wire, named, timestamped and placed in host time.

    The three timebases are kept side by side on purpose. `raw` is what the
    device said and is the evidence; `unwrapped` is that made monotonic for this
    connection; `host_time` is an estimate and is labelled as one, and is None
    until a `ping` has been answered. dev/DAEMON.md 4.6 keeps all three in the
    trace for the same reason.
    """

    trial_id: int
    sequence_number: int
    visit: StateVisitRecord
    unwrapped_device_microseconds: int
    host_time: HostTimeEstimate | None


class DeviceSupervisor:
    """One MCU, for as long as the daemon is running."""

    def __init__(
        self,
        target: str = DEFAULT_TARGET,
        line_map: LineMap | None = None,
        *,
        baud: int = DEFAULT_BAUD,
        timeout: float = DEFAULT_TIMEOUT,
        session_seed: str | None = None,
        expected_board: str = "",
        on_state_visit: Callable[[ObservedStateVisit], None] | None = None,
        on_unsolicited_message: Callable[[dict], None] | None = None,
        on_trial_result: Callable[[TrialResultRecord], None] | None = None,
        on_line_observed: Callable[[str, str], None] | None = None,
    ):
        self.target = target
        self.line_map = line_map if line_map is not None else LineMap()

        #: What board this rig is supposed to have, or "" for "do not check".
        #: See `_refuse_a_board_this_rig_is_not_wired_for`.
        self.expected_board = expected_board
        self.baud = baud
        self.timeout = timeout

        #: Fixed for the life of the supervisor when given, so that a whole
        #: session -- including one interrupted by a reconnect -- can be
        #: replayed. Drawn fresh per connection when it is not.
        self.configured_session_seed = session_seed

        #: Every line crossing the wire, for the serial monitor. Passed down to
        #: each `SerialLink` this opens rather than held here, because the
        #: transport is the only place that sees a line before anything has
        #: decided whether it means anything.
        self.on_line_observed = on_line_observed
        self.on_state_visit = on_state_visit or (lambda observed: None)
        self.on_unsolicited_message = on_unsolicited_message or (lambda message: None)
        #: Called when a whole result has been collected by `pump_incoming_lines`.
        #: A daemon does not sit waiting for one -- a result arrives unasked, in
        #: the middle of whatever else the link is doing.
        self.on_trial_result = on_trial_result or (lambda result: None)
        self._result_collector = TrialResultCollector()

        self._link: SerialLink | None = None
        self._session: RequestResponseSession | None = None
        self.hello_ack: dict | None = None
        self.capabilities: DeviceCapabilities | None = None
        self.session_seed: str | None = None
        #: What the board says its pins are called (dev/PROTOCOL.md §3.6), or
        #: what this daemon assumed when the board could not say. Read once per
        #: connection, because it cannot change without a reflash -- and a
        #: reflash is a reconnect.
        self.pin_map = DevicePinMap()
        #: The configured map with every `line_index` resolved and checked
        #: against the board. This is what is pushed and what graphs compile
        #: against; `line_map` is what somebody wrote in the config file.
        self.resolved_line_map = self.line_map

        self.committed_graph_set: CompiledGraphSet | None = None
        self._graphs_of_the_committed_set: list[GraphDefinition] = []

        self.clock = DeviceClockCorrelation()
        self.armed_trial_id: int | None = None
        self.armed_graph_name: str | None = None
        #: The graph a self-driving board was pointed at, which is what names
        #: the results of runs this daemon did not arm. Kept apart from
        #: `armed_graph_name` because the two answer different questions: one is
        #: "what did I arm", the other "what is the board doing on its own".
        self.autorun_graph_name: str | None = None

        #: Every reconnection, counted. A link that flaps should be visible to
        #: whoever is debugging the rig rather than inferred from trials that
        #: did not happen -- the same argument `dropped_lines` won on the wire.
        self.connection_count = 0

    # ------------------------------------------------------------ the link ---

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    def connect_and_watch(self) -> None:
        """Open the port and say nothing.

        For a board that is running on its own (dev/PROTOCOL.md 3.7): greeting
        it would *take the rig* -- cancelling the run in flight and stopping it
        driving itself -- and there are times when what is wanted is to watch,
        not to take over. The results and visits it emits are routed exactly as
        they are in a greeted session, so a daemon can record an unattended
        session it is not running.

        Nothing else works on this connection. Every command but `hello` is
        refused by a device nobody has greeted, which is the rule that makes
        this safe rather than a way to half-connect: call `connect_and_greet`
        when the point is to take the rig.
        """
        self.disconnect()
        link = SerialLink(
            self.target,
            baud=self.baud,
            timeout=self.timeout,
            on_line_observed=self.on_line_observed,
        )
        link.reset_input()
        self._link = link
        self._session = RequestResponseSession(
            link,
            on_unsolicited=self._handle_unsolicited_message,
            on_junk=lambda line, why: None,
        )
        self.clock.forget_everything_observed()
        self.connection_count += 1

    def connect_and_greet(self) -> dict:
        """Open the port, say hello, and push this rig's wiring.

        In that order and not another. The greeting is what takes the rig from a
        board that was arming its own trials, and the wiring is what makes
        `fail_safe()` correct for *this* box -- so it goes before any graph and
        long before any trial.
        """
        self.disconnect()
        link = SerialLink(
            self.target,
            baud=self.baud,
            timeout=self.timeout,
            on_line_observed=self.on_line_observed,
        )
        link.reset_input()
        session = RequestResponseSession(
            link,
            on_unsolicited=self._handle_unsolicited_message,
            on_junk=lambda line, why: None,
        )

        self.session_seed = self.configured_session_seed or random_seed()
        hello_ack = session.hello(seed=self.session_seed, timeout=self.timeout)

        self._link = link
        self._session = session
        self.hello_ack = hello_ack
        self.capabilities = DeviceCapabilities.from_hello_ack(hello_ack)
        # A reset device restarts its clock from zero, so an offset measured
        # before the reconnect would be wrong by however long the board was
        # away -- and wrong plausibly, which is the worst kind.
        self.clock.forget_everything_observed()
        self.connection_count += 1
        self.armed_trial_id = None
        self.armed_graph_name = None

        # Before the pin map, because the pin map is the thing that would
        # otherwise make the wrong board look right.
        try:
            self._refuse_a_board_this_rig_is_not_wired_for(hello_ack)
        except ValueError:
            self.disconnect()
            raise

        # Before the wiring, because the wiring is a set of masks over line
        # numbers and this is what says which number is which pin. A line map
        # that does not match the board is refused here -- with the link closed
        # again -- rather than pushed: masks built from a wrong index are a
        # valve driven from a lever's line, and nothing downstream would say so.
        self.pin_map = self.read_pin_map()
        try:
            self.resolved_line_map = self.line_map.resolved_against(self.pin_map)
        except ValueError:
            self.disconnect()
            raise

        self.push_wiring()
        return hello_ack

    def _refuse_a_board_this_rig_is_not_wired_for(self, hello_ack: dict) -> None:
        """Stop here if this is not the board the rig config names.

        A line map is checked against the pins the board reports, which catches
        a pin that does not exist -- and misses the case that matters most,
        because pin *names* repeat across boards. A Teensy 4.1 has an `A0` and
        so does an R4 Minima, they are not the same hole, and a map written for
        one resolves perfectly against the other. Nothing downstream would
        notice: the indices are valid, the wiring pushes, the graphs upload, and
        the first sign is an animal being rewarded by a lamp.

        So the rig config says which board it is wired for and this refuses
        anything else. Empty means the check is off, which is what a bench
        wants when it swaps a board for the native device on a socket.
        """
        if not self.expected_board:
            return
        board = str(hello_ack.get("board") or "")
        if board == self.expected_board:
            return
        raise ValueError(
            f"this rig is configured for a {self.expected_board!r} board and the device on "
            f"{self.target} says it is a {board or '(unnamed)'!r}. Its pin names may look "
            f"right and mean different holes, so nothing is pushed to it. Change "
            f"expected_board in the rig config, or plug in the board this rig is wired for"
        )

    def disconnect(self) -> None:
        if self._link is not None:
            self._link.close()
        self._link = None
        self._session = None

    def reconnect_and_restore(self) -> dict:
        """Come back after a link loss, and put the device back as it was.

        The committed set survives a reconnect -- that is what `hello_ack`'s
        `has_set` and `set_version` are for, and why a bridge restarting does
        not cost a re-upload. So this re-uploads only when the board came back
        without the set this supervisor believes in.
        """
        hello_ack = self.connect_and_greet()
        if self.committed_graph_set is None:
            return hello_ack
        if not self._device_holds_the_committed_set(hello_ack):
            self.upload_graph_set(
                self._graphs_of_the_committed_set,
                set_version=self.committed_graph_set.set_version,
            )
        return hello_ack

    def _device_holds_the_committed_set(self, hello_ack: dict) -> bool:
        if self.committed_graph_set is None:
            return False
        return bool(hello_ack.get("has_set")) and hello_ack.get("set_version") == (
            self.committed_graph_set.set_version
        )

    def _require_session(self) -> RequestResponseSession:
        if self._session is None:
            raise DeviceNotConnected(f"no link to {self.target} is open")
        return self._session

    # -------------------------------------------------------- the pin map ---

    def read_pin_map(self) -> DevicePinMap:
        """Ask the board what its pins are called. Never fatal.

        Two requests, one per direction, because §3.6 answers one at a time --
        both directions do not fit one line on a board with many lines, and a
        reply carrying half a map would be worse than none.

        A board that refuses -- `no_pin_map`, or `unknown_type` from any
        firmware flashed before the command existed -- is not an error. It is
        the common case in a rack that has not been reflashed yet, and the
        daemon falls back to `board_pin_labels.py` **and says that it did**.
        """
        session = self._require_session()
        try:
            inputs = session.request(MsgType.PINS, timeout=self.timeout, dir="in")
            outputs = session.request(MsgType.PINS, timeout=self.timeout, dir="out")
        except DeviceRefusedTheCommand:
            return self._pin_map_this_daemon_assumes()
        return DevicePinMap(
            input_pin_labels=[str(label) for label in inputs.get("pins", [])],
            output_pin_labels=[str(label) for label in outputs.get("pins", [])],
            source="device",
        )

    def _pin_map_this_daemon_assumes(self) -> DevicePinMap:
        """The host's own table, for a board that cannot answer for itself.

        Marked `assumed` all the way up to the UI. It is a hand-copied pin map,
        which is the thing §3.6 exists to stop being the only option -- so it is
        used, and it is never presented as the board's word.
        """
        board = (self.hello_ack or {}).get("board", "")
        labels = PIN_MAPS.get(board or "", {})
        if not labels:
            return DevicePinMap(source="unknown")
        return DevicePinMap(
            input_pin_labels=list(labels.get("in", [])),
            output_pin_labels=list(labels.get("out", [])),
            source="assumed",
        )

    # --------------------------------------------------------- the wiring ---

    def push_wiring(self) -> dict:
        """Tell the board what it is wired to. dev/PROTOCOL.md 3.5."""
        session = self._require_session()
        return session.request(
            MsgType.WIRING, timeout=self.timeout, **self.resolved_line_map.wiring_message_fields()
        )

    # ------------------------------------------------------- the graph set ---

    def upload_graph_set(
        self, graphs: list[GraphDefinition], set_version: int
    ) -> CompiledGraphSet:
        """Compile the session's graphs and put the whole set on the device.

        The slow call, and the one where a session is allowed to fail: a graph
        too big for this board is refused here, minutes before an animal is in
        the booth, rather than at trial 40. See dev/DAEMON.md 4.3.
        """
        session = self._require_session()
        if self.capabilities is None:
            raise DeviceNotConnected("the device has not been greeted, so its caps are unknown")

        compiled = compile_graph_set_for_device(
            graphs, self.resolved_line_map, self.capabilities, set_version
        )
        # Not committed on this side until the device says set_ok. From
        # set_begin until then the board holds no graph at all (PROTOCOL.md
        # 3.2), so believing otherwise here would be believing something the
        # board would contradict.
        self.committed_graph_set = None
        send_compiled_upload_messages(session, compiled.upload_messages, timeout=self.timeout)
        self.committed_graph_set = compiled
        self._graphs_of_the_committed_set = list(graphs)
        return compiled

    def _require_committed_graph_set(self) -> CompiledGraphSet:
        if self.committed_graph_set is None:
            raise NoGraphSetCommitted(
                "no graph set has been uploaded, so no trial can name a graph"
            )
        return self.committed_graph_set

    # -------------------------------------------------------- a trial, once ---

    def configure_trial(
        self,
        trial_id: int,
        graph_name: str,
        *,
        cap_milliseconds: int = 0,
        start_source: str = "serial",
        distribution_patches: list[dict] | None = None,
    ) -> dict:
        """Arm the device for one trial of one graph.

        `graph_name`, never an index. The daemon built the set, so the daemon
        knows which slot the name is in -- and an index on the caller's side
        would be a cache to get wrong across a re-upload.
        """
        session = self._require_session()
        compiled = self._require_committed_graph_set()
        fields: dict[str, object] = {
            "trial_id": trial_id,
            "set_version": compiled.set_version,
            "graph_index": compiled.slot_for_graph_name(graph_name),
            "start": start_source,
        }
        if cap_milliseconds:
            fields["cap_ms"] = cap_milliseconds
        if distribution_patches:
            fields["patch"] = distribution_patches

        armed = session.request(MsgType.CONFIGURE, timeout=self.timeout, **fields)
        self.armed_trial_id = trial_id
        self.armed_graph_name = graph_name
        return armed

    def start_trial(self, trial_id: int) -> dict:
        session = self._require_session()
        return session.request(MsgType.START, timeout=self.timeout, trial_id=trial_id)

    def cancel_trial(self, trial_id: int) -> dict:
        """Ask for a cancel, and report whatever actually happened.

        A cancel that races a terminal state comes back with the **real**
        outcome. That is passed through unchanged: asking to cancel and being
        told `HIT` is the caller's to cope with, and the alternative is a record
        claiming a trial was cancelled when the animal had already responded.
        """
        session = self._require_session()
        return session.request(
            MsgType.CANCEL, timeout=self.timeout, trial_id=trial_id, reason="host"
        )

    # ------------------------------------------------- the board on its own ---

    def set_autorun(
        self,
        enabled: bool,
        *,
        graph_name: str | None = None,
        cap_milliseconds: int = 0,
        seed: int | None = None,
        first_trial_id: int | None = None,
        start_now: bool = True,
    ) -> dict:
        """Hand the board the job of arming its own trials, or take it back.

        The one thing this daemon does that makes itself optional. See
        dev/PROTOCOL.md 3.7: the device starts each run itself and takes the
        interval between them from the dwell the terminal state it reached
        declared, which is why the timing lives in the graph and only the
        authority lives here.

        `graph_name`, not an index, for the same reason `configure_trial` takes
        one. Autorun cannot switch paradigms afterwards -- switching is a
        decision, and the premise is that nothing is making decisions.

        `start_now` false records that this board should drive itself without
        starting it -- which is how a rig is set up, because `save_settings` is
        refused on a board that is running and a board arming its own trials is
        never idle. Enable, save, power cycle.
        """
        session = self._require_session()
        fields: dict[str, object] = {"enabled": enabled}
        if graph_name is not None:
            compiled = self._require_committed_graph_set()
            fields["graph_index"] = compiled.slot_for_graph_name(graph_name)
        if cap_milliseconds:
            fields["cap_ms"] = cap_milliseconds
        if seed is not None:
            fields["seed"] = f"{seed:016X}"
        if first_trial_id is not None:
            fields["first_trial_id"] = first_trial_id
        if not start_now:
            fields["start_now"] = False
        reply = session.request(MsgType.AUTORUN, timeout=self.timeout, **fields)
        # Remembered so that the results of runs this daemon did not arm can
        # still be read: the device reports state indices, and only the graph
        # gives them names.
        if enabled:
            if graph_name is not None:
                self.autorun_graph_name = graph_name
        else:
            self.autorun_graph_name = None
        return reply

    def read_autorun(self) -> dict:
        """What the board would do on its own, asked rather than remembered.

        The settings outlive the session that set them and survive a daemon that
        greets and thereby takes the rig, so this is a question a freshly
        connected daemon genuinely has.
        """
        return self._require_session().request(MsgType.AUTORUN, timeout=self.timeout)

    def save_settings(self) -> dict:
        """Write the board's wiring, graph set and autorun settings to its own
        storage, so that all three survive a power cut. dev/PROTOCOL.md 3.8.

        Slow by the standards of everything else here -- it erases and programs
        data flash -- and refused by the device while a trial is running rather
        than stalling the scan. The reply carries `write_count`, which is flash
        wear made visible.
        """
        return self._require_session().request(MsgType.SAVE, timeout=max(self.timeout, 5.0))

    def wait_for_trial_result(self, timeout: float = 15.0) -> TrialResultRecord:
        """Collect the chunked result and read it back into names.

        Named against the graph that **actually ran** -- the compiled set this
        supervisor uploaded -- rather than against whatever the store holds
        today, which is what keeps a renamed state from mislabelling last week's
        data.
        """
        session = self._require_session()
        compiled = self._require_committed_graph_set()
        # The trial this daemon armed, or the graph a self-driving board was
        # pointed at: under autorun the device arms its own trials, and its
        # results are still this daemon's to read.
        graph_name = self._graph_name_for_reporting()
        if graph_name is None:
            raise NoGraphSetCommitted("no trial has been configured on this connection")
        compiled_graph = compiled.graph_named(graph_name)

        assert compiled_graph is not None  # named above, for the reader
        return self._name_a_reassembled_result(read_trial_result(session, timeout=timeout))

    def _graph_name_for_reporting(self) -> str | None:
        """Whose graph the run that just ended was: the trial this daemon armed,
        or -- for a board arming its own -- the graph autorun was pointed at."""
        return self.armed_graph_name or self.autorun_graph_name

    def _name_a_reassembled_result(
        self, reassembled: ReassembledTrialResult
    ) -> TrialResultRecord:
        """Turn a result's indices into the names of the graph that ran it."""
        compiled = self._require_committed_graph_set()
        graph_name = self._graph_name_for_reporting()
        if graph_name is None:
            raise NoGraphSetCommitted("no trial has been configured on this connection")
        compiled_graph = compiled.graph_named(graph_name)
        begin = reassembled.begin
        return TrialResultRecord(
            trial_id=begin["trial_id"],
            outcome=TrialOutcome(begin["outcome"]),
            cancel_reason=TrialCancelReason(begin.get("cancel_reason", 0)),
            total_duration_microseconds=begin.get("total_us", 0),
            visits=[
                decode_state_visit_row(
                    row,
                    compiled_graph.state_names_by_index,
                    compiled_graph.transition_target_names_by_state_index,
                )
                for row in reassembled.rows
            ],
            path_was_truncated=bool(begin.get("truncated", False)),
            first_visit_sequence_number=begin.get("first_seq", 0),
            total_visit_count=begin.get("total_visits", len(reassembled.rows)),
        )

    def run_trial_to_completion(
        self,
        trial_id: int,
        graph_name: str,
        *,
        cap_milliseconds: int = 0,
        result_timeout: float = 15.0,
        **configure_options,
    ) -> TrialResultRecord:
        """configure, start, and collect. The whole loop, for callers that want it."""
        self.configure_trial(
            trial_id, graph_name, cap_milliseconds=cap_milliseconds, **configure_options
        )
        self.start_trial(trial_id)
        return self.wait_for_trial_result(timeout=result_timeout)

    # ------------------------------------------------------------ the clock ---

    def send_heartbeat_ping(self) -> dict:
        """Arm the device's link-loss watchdog, and take a clock reading.

        One call doing two jobs, and not as a trick: the watchdog wants a `ping`
        at some cadence anyway, and a `ping` round trip is exactly the
        observation the clock correlation needs. Making them separate would mean
        pinging twice as often for no gain.
        """
        session = self._require_session()
        host_seconds_before = time.time()
        pong = session.request(MsgType.PING, timeout=self.timeout)
        host_seconds_after = time.time()
        if "us" in pong:
            self.clock.observe_ping_round_trip(
                host_seconds_before, int(pong["us"]), host_seconds_after
            )
        return pong

    def read_state_report(self) -> dict:
        return self._require_session().state(timeout=self.timeout)

    def pump_incoming_lines(self, budget_seconds: float = 0.05) -> int:
        """Read whatever the device has said, for a bounded moment.

        The daemon's read path, and the counterpart of `wait_for_trial_result`:
        that one sits until a result arrives, which is right for a bench script
        and impossible for a process that also has an API to answer. This
        returns after `budget_seconds` whatever happened, so the thread calling
        it can give the link back.

        Do not mix the two on one connection. Both consume lines, and a result
        half-collected by one of them cannot be finished by the other.
        """
        session = self._require_session()
        deadline = time.monotonic() + budget_seconds
        lines_read = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # The budget is the read's timeout, not a loop condition around a
            # blocking read: this call holds the device lock, so a read that
            # waited for the *command* timeout would make every request queue
            # behind an idle link.
            line = session.link.read_line(timeout=remaining)
            if line is None:
                break
            lines_read += 1
            self._route_one_incoming_line(session, line)
        return lines_read

    def _route_one_incoming_line(self, session: RequestResponseSession, line: str) -> None:
        message = session.receive(line)
        if message is None:
            # Unsolicited, junk, or blank -- `receive` has already routed it to
            # the sinks this supervisor installed. Result chunks are unsolicited
            # by that rule, so they come back here rather than being returned.
            return
        # A reply to a command nobody is waiting for: a command that timed out
        # and whose answer arrived late. Worth seeing rather than swallowing.
        self.on_unsolicited_message(message)

    def _collect_result_chunk(self, line: str, message: dict) -> None:
        """Feed one `result_*` line to the collector, and report a whole one."""
        try:
            reassembled = self._result_collector.feed(line, message)
        except ValueError as exc:
            self.on_unsolicited_message({"msg_type": "error", "message": str(exc)})
            return
        if reassembled is None:
            return
        try:
            named = self._name_a_reassembled_result(reassembled)
        except NoGraphSetCommitted:
            # A result from a run this daemon did not configure -- a board that
            # was already driving itself when this connection opened, or one
            # this daemon is only watching. Real, and unreadable without the
            # graph, so it goes on as an unsolicited message rather than being
            # decoded into a guess. Same rule as the visit stream below.
            self.on_unsolicited_message(reassembled.begin)
            return
        self.on_trial_result(named)

    # ------------------------------------------------------ the visit stream ---

    def _handle_unsolicited_message(self, message: dict, line: str = "") -> None:
        """Route what the device says without being asked.

        `visit` is decoded here because this is the only place that holds both
        halves: the compiled graph that gives an index a name, and the clock
        that gives a device microsecond a host time. Result chunks go to the
        collector. Everything else is handed on untouched.
        """
        message_type = message.get(Field.MSG_TYPE)
        if message_type in (MsgType.RESULT_BEGIN, MsgType.RESULT_PATH, MsgType.RESULT_END):
            if line:
                self._collect_result_chunk(line, message)
            return
        if message_type != MsgType.VISIT:
            self.on_unsolicited_message(message)
            return
        observed = self._decode_visit_message(message)
        if observed is not None:
            self.on_state_visit(observed)

    def _decode_visit_message(self, message: dict) -> ObservedStateVisit | None:
        compiled = self.committed_graph_set
        graph_name = self._graph_name_for_reporting()
        if compiled is None or graph_name is None:
            # A visit from a run this daemon did not configure -- a board that
            # was already arming its own trials when this connection opened, or
            # a line-started trial before anything named a graph. It is real and
            # it is unreadable without a graph, so it goes on as an unsolicited
            # message rather than being decoded into a guess.
            self.on_unsolicited_message(message)
            return None

        compiled_graph = compiled.graph_named(graph_name)
        visit = decode_state_visit_row(
            message["v"],
            compiled_graph.state_names_by_index,
            compiled_graph.transition_target_names_by_state_index,
        )
        # Unwrapped here, once, in arrival order: the stream is the only place
        # in the daemon that sees every device timestamp exactly once and in
        # sequence, which is what makes unwrapping correct at all.
        unwrapped = self.clock.unwrap_device_microseconds(visit.entered_device_microseconds)
        return ObservedStateVisit(
            trial_id=int(message.get("trial_id", 0)),
            sequence_number=int(message.get("seq", 0)),
            visit=visit,
            unwrapped_device_microseconds=unwrapped,
            host_time=self.clock.host_time_for_unwrapped_device_microseconds(unwrapped),
        )

    # -------------------------------------------------------------- context ---

    def __enter__(self) -> "DeviceSupervisor":
        self.connect_and_greet()
        return self

    def __exit__(self, *exception) -> None:
        self.disconnect()


__all__ = [
    "DeviceNotConnected",
    "DeviceRefusedTheCommand",
    "DeviceSupervisor",
    "NoGraphSetCommitted",
    "ObservedStateVisit",
    "PROTOCOL_VERSION",
]
