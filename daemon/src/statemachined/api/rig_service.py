# SPDX-License-Identifier: LGPL-3.0-or-later
"""What outlives a request: the device, the store, the trace, and the lock.

A router answers one question and forgets. This holds the things that cannot be
rebuilt per request -- an open port, a session seed, a committed graph set, a
trace ring -- and it is the only object in the daemon that touches all of them.

**One lock, and everything that talks to the device holds it.** A serial link is
not reentrant: two requests each writing a command would interleave two lines on
one wire and neither would get its reply. The lock is coarse on purpose. The
alternative -- a queue and a reader that matches replies to requests -- is the
right shape for a link that must serve many callers concurrently, and this one
serves triald in a strict request/response loop plus a browser tab. Coarse
locking is honest about that; a queue would be machinery for a concurrency this
rig does not have.

**A thread reads the link between commands**, because a result and the visit
stream arrive *unasked*. Without it they would sit in the kernel's buffer until
the next command happened to read them, and a trace whose timestamps are the
device's but whose arrival is whenever somebody next asked is not a trace.
"""

from __future__ import annotations

import threading
import time

from ..device.device_line_monitor import DeviceLineMonitor
from ..device.device_supervisor import DeviceSupervisor, ObservedStateVisit
from ..device.state_visit_trace import (
    KIND_ACTIVE_GRAPH_SELECTED,
    KIND_AUTORUN_CHANGED,
    KIND_CONFIG_LOADED,
    KIND_GRAPH_SET_UPLOADED,
    KIND_LINK_CONNECTED,
    KIND_LINK_LOST,
    KIND_RECORDING_CLEARED,
    KIND_RECORDING_PAUSED,
    KIND_RECORDING_RESUMED,
    KIND_RECORDING_STARTED,
    KIND_RECORDING_STOPPED,
    KIND_SEQUENCE_GAP,
    KIND_SESSION_CLOSED,
    KIND_SESSION_OPENED,
    KIND_SETTINGS_SAVED,
    KIND_STATE_VISIT,
    KIND_TRIAL_CANCELLED,
    KIND_TRIAL_CONFIGURED,
    KIND_TRIAL_RESULT,
    KIND_TRIAL_STARTED,
    StateVisitTrace,
)
from ..event_recording import EventRecorder
from ..graph_set_compiler import CompiledGraphSet
from ..graph_store import GraphStore
from ..model.graph_definition import GraphDefinition
from ..model.line_map import LineMap
from ..model.state_machine_config import StateMachineConfig
from ..model.trial_record import TrialResultRecord
from ..rig_configuration import RigConfiguration
from ..state_machine_config_store import StateMachineConfigStore
from ..triald_client import TrialdClient


class NoConfigLoaded(RuntimeError):
    """A session was asked for and nothing says what this rig is wired like."""


class NoActiveGraph(RuntimeError):
    """A trial named no graph and the rig has not been told which one to use."""


class GraphNotInTheLoadedConfig(RuntimeError):
    """A graph was selected that the loaded config does not carry."""


class RigService:
    """One rig: one device, one store, one trace, and the config it is running.

    **Two configurations, and the difference is which of them this object may
    write.** `self.configuration` is the rig config -- the box, from
    `/etc/braemons/statemachined-rig-config.toml` -- and nothing here ever
    writes it back. `self.state_machine_config` is the line map and the graphs,
    loaded from the store below, edited from the web UI and saved back to
    `/var/lib/braemons/statemachined/configs/`.
    """

    def __init__(self, configuration: RigConfiguration):
        self.configuration = configuration
        self.graph_store = GraphStore(configuration.graph_store_directory)
        self.state_machine_config_store = StateMachineConfigStore(
            configuration.state_machine_config_directory
        )
        #: What this rig is wired like and what it can run, or None before
        #: anybody has said. None is a real state and not a half-initialised
        #: one: a daemon whose board is plugged in but whose experiment nobody
        #: has chosen yet is exactly a rig on a bench in the morning.
        self.state_machine_config: StateMachineConfig | None = None
        #: When the graphs went up, as the session's own record of itself.
        #: None means no session is open -- the board may still hold a
        #: committed set from before, which `GET /api/session` says plainly
        #: rather than pretending either way.
        self.session_opened_at: float | None = None
        self.trace = StateVisitTrace(
            configuration.trace_ring_entries, configuration.trace_directory
        )
        #: A named selection out of the trace, for a rig with no triald writing
        #: a `.tdr`. A sink rather than a poller, so it cannot miss an entry the
        #: ring evicted before anybody asked -- see `event_recording.py`.
        self.recorder = EventRecorder(configuration.recording_directory)
        self.trace.add_sink(self.recorder.record)
        #: Which graph a trial gets when it does not name one. The web UI's
        #: "run a trial" is the caller that needs this; triald names a graph per
        #: trial and never touches it, which is why this is a default and not a
        #: mode -- an explicit `graph` always wins.
        self.active_graph_name: str | None = None
        self.triald = TrialdClient(configuration.triald_base_url)
        #: The wire itself, both directions, for as long as the ring holds it.
        #: Always on: a link fault that happens once an hour is not reproducible
        #: on demand, and a monitor somebody has to switch on first is one that
        #: is off when the interesting thing happens.
        self.line_monitor = DeviceLineMonitor()

        self.supervisor = DeviceSupervisor(
            configuration.device_target,
            # Empty until a state-machine config is loaded. A rig with no
            # config has no line map, and inventing one -- eight lines called
            # `input_0` -- would be a guess at the one thing that cannot be
            # guessed: which pin the lever is on.
            LineMap(),
            baud=configuration.device_baud,
            timeout=configuration.device_timeout_seconds,
            session_seed=configuration.session_seed or None,
            expected_board=configuration.expected_board,
            on_state_visit=self._record_state_visit,
            on_trial_result=self._record_trial_result,
            on_line_observed=self.line_monitor.record,
        )

        #: Held by everything that talks to the device. See the note above.
        self.device_lock = threading.RLock()

        self.last_trial_result: TrialResultRecord | None = None
        self.last_error_from_the_device: str | None = None
        #: The device's own per-run sequence number, to notice a dropped visit.
        #: A gap is what makes one *detectable* rather than a hole nobody sees.
        self._last_visit_sequence_number: int | None = None

        self._background_thread: threading.Thread | None = None
        self._stop_background = threading.Event()
        self._last_heartbeat_seconds = 0.0

    # ------------------------------------------------------- the lifecycle ---

    def start(self) -> None:
        """Load the configured config, connect if told to, read the link.

        The config first, so the line map exists before the greeting: the
        wiring is pushed as part of connecting, and a board greeted with no map
        is a board holding every line at a default `safe` nobody chose.
        """
        self._load_the_startup_config()
        if self.configuration.connect_on_startup:
            try:
                self.connect()
            except Exception as exc:  # noqa: BLE001 -- reported, not fatal
                # A daemon that refused to start without a board would make the
                # rig unadministrable exactly when somebody needs the API to
                # find out why the board is missing.
                self.last_error_from_the_device = str(exc)
        self._stop_background.clear()
        self._background_thread = threading.Thread(
            target=self._read_the_link_forever, name="statemachined-link", daemon=True
        )
        self._background_thread.start()

    def _load_the_startup_config(self) -> None:
        """The config named in the rig config, if there is one and it loads.

        Never fatal, for the same reason a missing board is not: a daemon that
        refused to start because a config was deleted would take the API down
        with it -- and the API is how somebody finds out that the config was
        deleted.
        """
        config_name = self.configuration.startup_state_machine_config
        if not config_name:
            return
        try:
            self.load_state_machine_config(config_name)
        except Exception as exc:  # noqa: BLE001 -- reported, not fatal
            self.last_error_from_the_device = (
                f"the startup state-machine config {config_name!r} did not load: {exc}"
            )

    def stop(self) -> None:
        self._stop_background.set()
        if self._background_thread is not None:
            self._background_thread.join(timeout=2.0)
            self._background_thread = None
        with self.device_lock:
            self.supervisor.disconnect()

    def connect(self) -> dict:
        with self.device_lock:
            hello_ack = self.supervisor.connect_and_greet()
        self.trace.append(
            KIND_LINK_CONNECTED,
            target=self.configuration.device_target,
            board=hello_ack.get("board"),
            firmware_version=hello_ack.get("fw"),
            connection_count=self.supervisor.connection_count,
        )
        return hello_ack

    def _read_the_link_forever(self) -> None:
        """The only thread that reads the device when nobody asked it to.

        Short bursts under the lock, so a request never waits long for it. A
        heartbeat `ping` goes out on the configured cadence: it arms the
        device's link-loss watchdog, and for free it keeps the clock
        correlation fresh.
        """
        while not self._stop_background.is_set():
            if not self.supervisor.is_connected:
                time.sleep(0.2)
                continue
            try:
                with self.device_lock:
                    self.supervisor.pump_incoming_lines(budget_seconds=0.05)
                    self._send_heartbeat_if_due()
            except Exception as exc:  # noqa: BLE001
                self._note_the_link_went_away(exc)
            time.sleep(0.005)

    def _send_heartbeat_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat_seconds < self.configuration.heartbeat_seconds:
            return
        self._last_heartbeat_seconds = now
        self.supervisor.send_heartbeat_ping()

    def _note_the_link_went_away(self, exc: Exception) -> None:
        self.last_error_from_the_device = str(exc)
        self.trace.append(KIND_LINK_LOST, detail=str(exc))
        with self.device_lock:
            self.supervisor.disconnect()

    # ------------------------------------------------------------ the trace ---

    def _record_state_visit(self, observed: ObservedStateVisit) -> None:
        """One `visit` into the ring, named and placed in host time.

        A gap in the device's per-run `seq` is recorded as its own entry rather
        than silently closed over. dev/DAEMON.md §3.6: the stream is a preview
        and the result is the record, so a gap here is a thing to reconcile at
        `result_end` -- but only if somebody wrote down that it happened.
        """
        expected = self._last_visit_sequence_number
        if expected is not None and observed.sequence_number > expected + 1:
            self.trace.append(
                KIND_SEQUENCE_GAP,
                trial_id=observed.trial_id,
                missing_from_sequence_number=expected + 1,
                missing_to_sequence_number=observed.sequence_number - 1,
            )
        self._last_visit_sequence_number = observed.sequence_number

        host_time = observed.host_time
        self.trace.append(
            KIND_STATE_VISIT,
            trial_id=observed.trial_id,
            device_sequence_number=observed.sequence_number,
            graph=self.supervisor.armed_graph_name,
            set_version=(
                self.supervisor.committed_graph_set.set_version
                if self.supervisor.committed_graph_set
                else None
            ),
            state_name=observed.visit.state_name,
            exit_cause=observed.visit.exit_cause,
            fired_transition_target_state_name=(
                observed.visit.fired_transition_target_state_name
            ),
            drawn_duration_ms=observed.visit.drawn_duration_ms,
            measured_duration_microseconds=observed.visit.measured_duration_microseconds,
            entered_device_microseconds=observed.visit.entered_device_microseconds,
            unwrapped_device_microseconds=observed.unwrapped_device_microseconds,
            entered_host_time=host_time.host_time_iso8601 if host_time else None,
            host_time_uncertainty_microseconds=(
                host_time.uncertainty_microseconds if host_time else None
            ),
        )

    def _record_trial_result(self, result: TrialResultRecord) -> None:
        self.last_trial_result = result
        self._last_visit_sequence_number = None
        self.trace.append(
            KIND_TRIAL_RESULT,
            trial_id=result.trial_id,
            outcome=result.outcome.name,
            cancel_reason=result.cancel_reason.name,
            total_duration_microseconds=result.total_duration_microseconds,
            visit_count=len(result.visits),
            total_visit_count=result.total_visit_count,
            path_was_truncated=result.path_was_truncated,
        )
        self._report_the_outcome_to_triald(result)

    def _report_the_outcome_to_triald(self, result: TrialResultRecord) -> None:
        if not self.triald.is_configured:
            return
        try:
            self.triald.report_trial_outcome(result)
        except Exception as exc:  # noqa: BLE001
            # A trial whose outcome did not reach triald is a hole in the
            # session's record. The daemon cannot fix it, so it writes down that
            # it happened rather than letting the hole be silent.
            self.trace.append(
                KIND_SEQUENCE_GAP, trial_id=result.trial_id, detail=f"triald: {exc}"
            )

    # --------------------------------------------- the state-machine config ---

    def load_state_machine_config(self, config_name: str) -> StateMachineConfig:
        """Read one from the store and make it this rig's.

        Reading and applying are one call because a half-applied config is the
        state nobody can reason about: a line map from Tuesday and graphs from
        Thursday, compiled against each other, with the names lining up by luck.
        """
        return self.apply_state_machine_config(
            self.state_machine_config_store.load(config_name)
        )

    def apply_state_machine_config(self, config: StateMachineConfig) -> StateMachineConfig:
        """Make this config the rig's, and push the wiring it implies.

        Resolved against the board **before** anything is kept, so a config
        naming a pin this board does not have is refused with the rig still
        running on the one it had (`model/line_map.py`). That is also where a
        config written for another board is caught, which is the case worth
        catching: a config is self-contained and therefore portable, and the one
        thing in it that is not portable is the map.

        The graphs are *not* uploaded here. Loading says what this rig is and
        can run; `open_session` is what puts it on the device, and keeping them
        apart is what lets somebody load a config to look at it without
        disturbing a board mid-experiment.
        """
        if self.supervisor.is_connected:
            resolved = config.line_map.resolved_against(self.supervisor.pin_map)
            self.supervisor.line_map = config.line_map
            self.supervisor.resolved_line_map = resolved
            with self.device_lock:
                self.supervisor.push_wiring()
        else:
            self.supervisor.line_map = config.line_map
            self.supervisor.resolved_line_map = config.line_map

        self.state_machine_config = config
        self.trace.append(
            KIND_CONFIG_LOADED,
            state_machine_config=config.name,
            board=config.board or None,
            graph_names=[graph.name for graph in config.graphs],
            wiring_pushed=self.supervisor.is_connected,
        )
        return config

    def save_state_machine_config(self, config: StateMachineConfig) -> StateMachineConfig:
        """Write one to the store. Does not load it.

        Saving and loading are separate for the same reason reading and
        applying are one: "write this down" and "run this now" are different
        intentions, and a UI that could only save by also arming the rig would
        be a UI nobody edits during a session.
        """
        self.state_machine_config_store.save(config)
        return config

    def require_state_machine_config(self) -> StateMachineConfig:
        if self.state_machine_config is None:
            known = ", ".join(self.state_machine_config_store.stored_config_names()) or "(none)"
            raise NoConfigLoaded(
                f"this rig has no state-machine config loaded, so nothing says which pin is "
                f"which line or what it can run. Load one: {known}"
            )
        return self.state_machine_config

    # ---------------------------------------------------------- the session ---

    def open_session(self) -> tuple[CompiledGraphSet, int]:
        """Put the loaded config's graphs on the device. dev/DAEMON.md §3.2.

        This is what triald does at the top of a session and what the web UI's
        button does on a bench -- the same call, because a bench that exercised
        a different path would be a bench that proves nothing about the rig.
        """
        config = self.require_state_machine_config()
        compiled, elapsed_milliseconds = self._upload(config.graphs)
        self.session_opened_at = time.time()
        self.trace.append(
            KIND_SESSION_OPENED,
            state_machine_config=config.name,
            set_version=compiled.set_version,
            graph_names=[graph.name for graph in compiled.graphs_by_slot],
            opened_by="state_machine_config",
        )
        return compiled, elapsed_milliseconds

    def close_session(self) -> dict:
        """Say the session is over, and leave the device holding its set.

        **What this does not do is unload the board**, and that is deliberate.
        The committed set surviving is what makes a reconnect cheap (§3.2) and
        what lets a session resume after a daemon restart. Closing is the
        daemon's own bookkeeping plus one safety act: a trial still armed is
        cancelled, because an armed trial with nobody driving it is a rig that
        will run one more trial at whatever time somebody next touches a lever.
        """
        cancelled_trial_id = self.supervisor.armed_trial_id
        if cancelled_trial_id is not None and self.supervisor.is_connected:
            try:
                self.cancel_trial(cancelled_trial_id)
            except Exception as exc:  # noqa: BLE001 -- reported, never fatal
                self.last_error_from_the_device = str(exc)
        was_open = self.session_opened_at is not None
        self.session_opened_at = None
        self.trace.append(
            KIND_SESSION_CLOSED,
            state_machine_config=(
                self.state_machine_config.name if self.state_machine_config else None
            ),
            cancelled_trial_id=cancelled_trial_id,
            was_open=was_open,
        )
        return {"was_open": was_open, "cancelled_trial_id": cancelled_trial_id}

    # ------------------------------------------------------ the active graph ---

    def select_active_graph(self, graph_name: str | None) -> str | None:
        """Say which graph a trial gets when it does not name one.

        A **default, not a mode**: an explicit `graph` on `POST
        /api/trial/configure` always wins, so triald -- which names a graph per
        trial and has no reason to know this exists -- is unaffected by whatever
        somebody selected in a browser tab.

        Checked against the loaded config, and against the committed set if
        there is one, because the alternative is a selection that looks fine in
        the UI and refuses at the moment somebody presses "run a trial".
        """
        if graph_name is None:
            self.active_graph_name = None
            self.trace.append(KIND_ACTIVE_GRAPH_SELECTED, graph=None)
            return None

        config = self.require_state_machine_config()
        known = [graph.name for graph in config.graphs]
        if graph_name not in known:
            raise GraphNotInTheLoadedConfig(
                f"the loaded config {config.name!r} has no graph called {graph_name!r}. "
                f"Has: {', '.join(known) or '(none)'}"
            )
        committed = self.supervisor.committed_graph_set
        if committed is not None:
            on_the_board = [graph.name for graph in committed.graphs_by_slot]
            if graph_name not in on_the_board:
                raise GraphNotInTheLoadedConfig(
                    f"{graph_name!r} is in the config but not in the set the board is holding "
                    f"({', '.join(on_the_board)}). Open a session to put it there."
                )
        self.active_graph_name = graph_name
        self.trace.append(KIND_ACTIVE_GRAPH_SELECTED, graph=graph_name)
        return graph_name

    def graph_for_a_trial(self, graph_name: str | None) -> str:
        """The graph a trial names, or the active one, or a refusal saying so."""
        if graph_name:
            return graph_name
        if self.active_graph_name:
            return self.active_graph_name
        raise NoActiveGraph(
            "this trial named no graph and no graph is selected as the active one. Name one, "
            "or select an active graph first."
        )

    # -------------------------------------------------------- the recording ---
    #
    # The order of each pair below is the only subtle thing here: the lifecycle
    # entry is written so that it lands **inside** the recording it is about. A
    # pause is traced before the segment closes and a resume after the next one
    # opens, so a recording explains its own gaps rather than leaving a reader to
    # infer them. See `event_recording.py` for why nothing here holds two locks.

    def start_recording(
        self, name: str | None = None, description: str = ""
    ) -> dict:
        chosen = name or self.recorder.suggest_a_name()
        manifest = self.recorder.start(
            chosen,
            description=description,
            state_machine_config=(
                self.state_machine_config.name if self.state_machine_config else None
            ),
        )
        self.trace.append(
            KIND_RECORDING_STARTED,
            recording=chosen,
            state_machine_config=manifest["state_machine_config"],
        )
        return self.recorder.manifest_of(chosen)

    def pause_recording(self) -> dict:
        name = self.recorder.manifest_of_the_active_recording()["name"]
        self.trace.append(KIND_RECORDING_PAUSED, recording=name)
        return self.recorder.pause()

    def resume_recording(self) -> dict:
        manifest = self.recorder.resume()
        self.trace.append(KIND_RECORDING_RESUMED, recording=manifest["name"])
        return self.recorder.manifest_of(manifest["name"])

    def stop_recording(self) -> dict:
        name = self.recorder.manifest_of_the_active_recording()["name"]
        self.trace.append(KIND_RECORDING_STOPPED, recording=name)
        return self.recorder.stop()

    def clear_recording(self) -> dict:
        manifest = self.recorder.clear()
        # After the clear, so a cleared recording's first line says what it is:
        # an empty file with no explanation is one somebody has to guess at.
        self.trace.append(KIND_RECORDING_CLEARED, recording=manifest["name"])
        return self.recorder.manifest_of(manifest["name"])

    # ----------------------------------------------------------- the graphs ---

    def upload_session_graph_set(self, graph_names: list[str]) -> tuple[CompiledGraphSet, int]:
        """Compile, check against this board's caps, upload, commit.

        Graphs **from the store, by name**, which is the older half of this API
        and the one triald has always used. `open_session` is the other half:
        the same upload, over the graphs a state-machine config carries.

        A session is open afterwards either way. That matters for watching:
        when triald drives a rig it calls *this*, and a web UI that only knew
        about `open_session` would show "no session" beside a board running
        trials -- which is the one thing somebody watching over triald's
        shoulder must not be told.
        """
        uploaded = self._upload(self.graph_store.load_all(graph_names))
        compiled, _ = uploaded
        self.session_opened_at = time.time()
        self.trace.append(
            KIND_SESSION_OPENED,
            state_machine_config=(
                self.state_machine_config.name if self.state_machine_config else None
            ),
            set_version=compiled.set_version,
            graph_names=[graph.name for graph in compiled.graphs_by_slot],
            opened_by="graph_names",
        )
        return uploaded

    def _upload(self, graphs: list[GraphDefinition]) -> tuple[CompiledGraphSet, int]:
        """Compile, check against this board's caps, upload, commit.

        Returns the compiled set and what it cost in milliseconds -- the slowest
        call in the API, and the one a UI shows a progress bar for.
        """
        started = time.monotonic()
        with self.device_lock:
            set_version = self._next_set_version()
            compiled = self.supervisor.upload_graph_set(graphs, set_version=set_version)
        elapsed_milliseconds = int((time.monotonic() - started) * 1000)
        self.trace.append(
            KIND_GRAPH_SET_UPLOADED,
            set_version=compiled.set_version,
            graph_names=[graph.name for graph in compiled.graphs_by_slot],
            elapsed_milliseconds=elapsed_milliseconds,
        )
        return compiled, elapsed_milliseconds

    def _next_set_version(self) -> int:
        """One more than the last, wrapping inside the wire's `u16`.

        The daemon's number, not a hash of the contents: `configure` carries it
        so that a set edit which did not land cannot leave the device
        confidently running the old paradigms, and for that it only has to
        *differ*.
        """
        committed = self.supervisor.committed_graph_set
        previous = committed.set_version if committed is not None else 0
        return (previous % 65535) + 1

    # ------------------------------------------------------------ the trial ---

    def configure_trial(self, **arguments) -> dict:
        with self.device_lock:
            armed = self.supervisor.configure_trial(**arguments)
        self.trace.append(
            KIND_TRIAL_CONFIGURED,
            trial_id=arguments.get("trial_id"),
            graph=arguments.get("graph_name"),
            set_version=armed.get("set_version"),
            graph_index=armed.get("graph_index"),
        )
        return armed

    def start_trial(self, trial_id: int) -> dict:
        with self.device_lock:
            started = self.supervisor.start_trial(trial_id)
        self.trace.append(
            KIND_TRIAL_STARTED, trial_id=trial_id, started_device_microseconds=started.get("at_us")
        )
        return started

    def cancel_trial(self, trial_id: int) -> dict:
        with self.device_lock:
            cancel_ack = self.supervisor.cancel_trial(trial_id)
        self.trace.append(
            KIND_TRIAL_CANCELLED,
            trial_id=trial_id,
            cancelled=cancel_ack.get("cancelled"),
            outcome_code=cancel_ack.get("outcome"),
        )
        return cancel_ack

    # --------------------------------------------------- the board on its own ---

    def set_autorun(self, enabled: bool, **arguments) -> dict:
        """Hand the board the job of arming its own trials, or take it back.

        The daemon becomes optional at this point, which is the whole intent:
        see dev/PROTOCOL.md 3.7. It is written to the trace because "who armed
        trial 412" is a question the record has to be able to answer, and a run
        the device armed itself looks otherwise identical to one this daemon did.
        """
        graph_name = arguments.get("graph_name")
        if enabled and graph_name is None:
            arguments["graph_name"] = self.graph_for_a_trial(None)
        with self.device_lock:
            reply = self.supervisor.set_autorun(enabled, **arguments)
        self.trace.append(
            KIND_AUTORUN_CHANGED,
            enabled=bool(reply.get("enabled")),
            active=bool(reply.get("active")),
            graph=arguments.get("graph_name"),
            next_trial_id=reply.get("next_trial_id"),
        )
        return reply

    def read_autorun(self) -> dict:
        """What the board would do on its own -- asked, not remembered.

        The settings outlive this daemon's session and survive a greeting that
        took the rig, so this is a question rather than a field.
        """
        if not self.supervisor.is_connected:
            return {}
        with self.device_lock:
            return self.supervisor.read_autorun()

    def save_device_settings(self) -> dict:
        """Write the board's wiring, graph set and autorun settings to its own
        storage, so that all three survive a power cut. dev/PROTOCOL.md 3.8."""
        with self.device_lock:
            saved = self.supervisor.save_settings()
        self.trace.append(
            KIND_SETTINGS_SAVED,
            has_set=bool(saved.get("has_set")),
            set_version=saved.get("set_version"),
            autorun=bool(saved.get("autorun")),
            write_count=saved.get("write_count"),
        )
        return saved

    # ------------------------------------------------------------ the state ---

    def read_device_state(self) -> dict:
        """One `state_report`, or what the daemon knows if there is no device."""
        if not self.supervisor.is_connected:
            return {}
        with self.device_lock:
            return self.supervisor.read_state_report()
