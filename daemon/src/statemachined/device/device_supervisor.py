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

from ..graph_set_compiler import (
    CompiledGraphSet,
    DeviceCapabilities,
    compile_graph_set_for_device,
)
from ..model.graph_definition import GraphDefinition
from ..model.line_map import LineMap
from ..model.trial_outcome import TrialCancelReason, TrialOutcome
from ..model.trial_record import StateVisitRecord, TrialResultRecord, decode_state_visit_row
from .device_clock_correlation import DeviceClockCorrelation, HostTimeEstimate
from .graph_set_upload import send_compiled_upload_messages
from .message_framing import DeviceRefusedTheCommand
from .message_vocabulary import Field, MsgType
from .request_response_session import PROTOCOL_VERSION, RequestResponseSession, random_seed
from .serial_link import DEFAULT_BAUD, DEFAULT_TARGET, DEFAULT_TIMEOUT, SerialLink
from .trial_result_reassembly import read_trial_result


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
        on_state_visit: Callable[[ObservedStateVisit], None] | None = None,
        on_unsolicited_message: Callable[[dict], None] | None = None,
    ):
        self.target = target
        self.line_map = line_map if line_map is not None else LineMap()
        self.baud = baud
        self.timeout = timeout

        #: Fixed for the life of the supervisor when given, so that a whole
        #: session -- including one interrupted by a reconnect -- can be
        #: replayed. Drawn fresh per connection when it is not.
        self.configured_session_seed = session_seed

        self.on_state_visit = on_state_visit or (lambda observed: None)
        self.on_unsolicited_message = on_unsolicited_message or (lambda message: None)

        self._link: SerialLink | None = None
        self._session: RequestResponseSession | None = None
        self.hello_ack: dict | None = None
        self.capabilities: DeviceCapabilities | None = None
        self.session_seed: str | None = None

        self.committed_graph_set: CompiledGraphSet | None = None
        self._graphs_of_the_committed_set: list[GraphDefinition] = []

        self.clock = DeviceClockCorrelation()
        self.armed_trial_id: int | None = None
        self.armed_graph_name: str | None = None

        #: Every reconnection, counted. A link that flaps should be visible to
        #: whoever is debugging the rig rather than inferred from trials that
        #: did not happen -- the same argument `dropped_lines` won on the wire.
        self.connection_count = 0

    # ------------------------------------------------------------ the link ---

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    def connect_and_greet(self) -> dict:
        """Open the port, say hello, and push this rig's wiring.

        In that order and not another. The greeting is what hands a bench board
        over from demo mode, and the wiring is what makes `fail_safe()` correct
        for *this* box -- so it goes before any graph and long before any trial.
        """
        self.disconnect()
        link = SerialLink(self.target, baud=self.baud, timeout=self.timeout)
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

        self.push_wiring()
        return hello_ack

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

    # --------------------------------------------------------- the wiring ---

    def push_wiring(self) -> dict:
        """Tell the board what it is wired to. dev/PROTOCOL.md 3.5."""
        session = self._require_session()
        return session.request(
            MsgType.WIRING, timeout=self.timeout, **self.line_map.wiring_message_fields()
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
            graphs, self.line_map, self.capabilities, set_version
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

    def wait_for_trial_result(self, timeout: float = 15.0) -> TrialResultRecord:
        """Collect the chunked result and read it back into names.

        Named against the graph that **actually ran** -- the compiled set this
        supervisor uploaded -- rather than against whatever the store holds
        today, which is what keeps a renamed state from mislabelling last week's
        data.
        """
        session = self._require_session()
        compiled = self._require_committed_graph_set()
        graph_name = self.armed_graph_name
        if graph_name is None:
            raise NoGraphSetCommitted("no trial has been configured on this connection")
        compiled_graph = compiled.graph_named(graph_name)

        raw_result = read_trial_result(session, timeout=timeout)
        visits = [
            decode_state_visit_row(
                row,
                compiled_graph.state_names_by_index,
                compiled_graph.transition_target_names_by_state_index,
            )
            for row in raw_result.rows
        ]
        begin = raw_result.begin
        return TrialResultRecord(
            trial_id=begin["trial_id"],
            outcome=TrialOutcome(begin["outcome"]),
            cancel_reason=TrialCancelReason(begin.get("cancel_reason", 0)),
            total_duration_microseconds=begin.get("total_us", 0),
            visits=visits,
            path_was_truncated=bool(begin.get("truncated", False)),
            first_visit_sequence_number=begin.get("first_seq", 0),
            total_visit_count=begin.get("total_visits", len(visits)),
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

    # ------------------------------------------------------ the visit stream ---

    def _handle_unsolicited_message(self, message: dict) -> None:
        """Route what the device says without being asked.

        `visit` is decoded here because this is the only place that holds both
        halves: the compiled graph that gives an index a name, and the clock
        that gives a device microsecond a host time. Everything else is handed
        on untouched.
        """
        if message.get(Field.MSG_TYPE) != MsgType.VISIT:
            self.on_unsolicited_message(message)
            return
        observed = self._decode_visit_message(message)
        if observed is not None:
            self.on_state_visit(observed)

    def _decode_visit_message(self, message: dict) -> ObservedStateVisit | None:
        compiled = self.committed_graph_set
        graph_name = self.armed_graph_name
        if compiled is None or graph_name is None:
            # A visit from a run this daemon did not configure -- demo mode, or
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
