# SPDX-License-Identifier: LGPL-3.0-or-later
"""`StatemachinedClient` — one rig's state machine, as methods.

Eight gRPC services in `proto/statemachined/v1/`, one object here. The services
are an organising device for the interface and not something a caller should
have to navigate, so this is flat: `rig.read_state()`, `rig.start_trial(7)`,
`rig.list_graphs()`.

**Flat is the change from the HTTP client this replaces**, which had eight
sub-objects — `rig.trial.start(7)`, `rig.graphs.list()` — mirroring the route
tree. The tree was the URL space, and the URL space is gone; keeping it would
have meant a caller learning a hierarchy that describes nothing.

Methods are named after what they ask for rather than after their rpcs, which
is the same rule the browser client keeps. Where the two differ, the rpc is
named in the docstring.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import TracebackType
from typing import Generic, TypeVar

import grpc

from . import _wire_conversions as convert
from ._grpc_transport import call, stream
from ._proto.statemachined.v1 import (
    device_pb2,
    documents_pb2,
    recording_pb2,
    service_pb2,
    service_pb2_grpc,
    session_pb2,
    state_pb2,
    trial_pb2,
)
from .api_types import (
    DEFAULT_OBSERVER_NAME,
    KIND_TRIAL_RESULT,
    Autorun,
    CancelTrialResult,
    CloseSessionResult,
    CommittedGraphSet,
    ConfigureTrialResult,
    DeviceState,
    DistributionPatch,
    FirmwareVersions,
    GraphSummary,
    GraphValidation,
    Health,
    LineMapView,
    LoadedConfigResult,
    Observers,
    OpenSessionResult,
    RecordingEntries,
    RecordingManifest,
    Recordings,
    RigConfiguration,
    RigConfigurationPatch,
    RigConfigurationUpdate,
    RigState,
    SaveSettingsResult,
    SerialMonitorEntry,
    SerialMonitorWindow,
    SessionState,
    StartTrialResult,
    StateMachineConfigSummaries,
    StoredFile,
    TraceEntry,
    TraceWindow,
    TrialResult,
    WriteLineMapResult,
)
from .daemon_refusals import DaemonIsUnavailable, DaemonRefusedTheRequest

#: Where this client connects: the daemon's one port, which is also what a
#: person types into a browser and what a console's `rigs.json` holds. The
#: panels, gRPC and gRPC-Web share it.
#:
#: It was one above, 8082, while the daemon was Python and `grpc.aio` needed a
#: socket of its own; the Rust daemon answered there too through the cutover,
#: and since the second port was dropped this is the only one.
DEFAULT_PORT = 8081

_T = TypeVar("_T")
_Wire = TypeVar("_Wire")


class DaemonStreamSubscription(Generic[_T]):
    """One open server-streaming rpc, as something to iterate and to close.

    A context manager because **the close matters**: a subscription the daemon
    is still feeding shows up in `read_observers()` and costs it work, and a
    script that opened one in a loop would leave a trail of them. Leaving the
    `with` cancels the call.

    Iterating raises
    :class:`~statemachined_client.daemon_refusals.DaemonRefusedTheRequest` —
    from the `next` that fails, not from the call that opened the stream,
    because that is where gRPC raises it. A cancel of our own is not a failure
    and ends the iteration quietly.
    """

    def __init__(self, open_call, one: Callable[[_Wire], _T]) -> None:
        self._call = open_call()
        self._one = one

    def __iter__(self) -> Iterator[_T]:
        for message in stream(lambda: self._call):
            yield self._one(message)

    def cancel(self) -> None:
        """Stop the subscription. Safe to call twice, and after iterating."""
        self._call.cancel()

    def __enter__(self) -> DaemonStreamSubscription[_T]:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.cancel()


class StatemachinedClient:
    """A connection to one statemachined daemon.

    ``address`` is ``host``, ``host:port`` or an empty string for localhost.
    One daemon is one board and one session, so no call carries a session id.

    **The port is the daemon's one port**, the same one its panels are served
    on — see :data:`DEFAULT_PORT`.

    Use it as a context manager, or call :meth:`close`::

        with StatemachinedClient("rig-3.local") as rig:
            rig.load_config("go-nogo-session")
            rig.open_session()

    **Every method may raise**
    :class:`~statemachined_client.daemon_refusals.DaemonRefusedTheRequest`.
    Nothing here returns an error code: a refusal carries a machine-readable
    `error`, the field to change, a sentence, and the gRPC status, and it is
    raised so that a script cannot proceed as though a command had worked.
    """

    def __init__(self, address: str = "", *, port: int = DEFAULT_PORT) -> None:
        self.address = _target(address, port)
        self._channel = grpc.insecure_channel(self.address)
        self._state = service_pb2_grpc.StateStub(self._channel)
        self._trial = service_pb2_grpc.TrialStub(self._channel)
        self._device = service_pb2_grpc.DeviceStub(self._channel)
        self._graphs = service_pb2_grpc.GraphStoreStub(self._channel)
        self._configs = service_pb2_grpc.StateMachineConfigStoreStub(self._channel)
        self._session = service_pb2_grpc.SessionStub(self._channel)
        self._recording = service_pb2_grpc.RecordingStub(self._channel)
        self._configuration = service_pb2_grpc.ConfigurationStub(self._channel)

    def close(self) -> None:
        self._channel.close()

    def __enter__(self) -> StatemachinedClient:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"StatemachinedClient({self.address!r})"

    def wait_until_ready(self, timeout_s: float = 10.0) -> None:
        """Block until the daemon answers, or raise `DaemonIsUnavailable`.

        A rig script that starts a daemon and talks to it immediately races the
        daemon's own startup; so does one that runs while a box is rebooting.
        This is the honest way to wait for that, rather than a `sleep` that is
        too short on the day it matters.

        **This says nothing about the board.** A daemon with no device answers
        every read and refuses the writes with
        :class:`~statemachined_client.daemon_refusals.NoBoardIsAttached`;
        `read_health().device_connected` is the question about the board.
        """
        try:
            grpc.channel_ready_future(self._channel).result(timeout=timeout_s)
        except grpc.FutureTimeoutError:
            raise DaemonIsUnavailable(
                "unavailable",
                "unavailable",
                f"no statemachined answered at {self.address} within {timeout_s:g}s",
            ) from None

    # -- is it up ---------------------------------------------------------------

    def read_health(self) -> Health:
        """Is the daemon up, and does it have a board. `Configuration/ReadHealth`.

        The only call that answers rather than refusing when there is no
        device, which is what makes it the one to ask first.
        """
        return convert.health_from_wire(
            call(lambda: self._configuration.ReadHealth(service_pb2.ReadHealthRequest()))
        )

    # -- what is happening ------------------------------------------------------

    def read_state(self) -> RigState:
        """The whole rig in one read. `State/ReadState`."""
        return convert.rig_state_from_wire(
            call(lambda: self._state.ReadState(service_pb2.ReadStateRequest()))
        )

    def watch_state(
        self, *, timeout_s: float | None = None
    ) -> DaemonStreamSubscription[RigState]:
        """The rig's state, as it changes. `State/WatchState`.

        The first frame is the state now, so a panel does not have to read and
        subscribe and then reconcile the two.

        `timeout_s` is a deadline on the **whole subscription**, not on one
        frame — that is what a gRPC deadline is. `None`, the default, is a
        stream that runs until something closes it, which is what a panel
        wants; a script that must not block for ever gives a number.
        """
        return DaemonStreamSubscription(
            lambda: self._state.WatchState(service_pb2.WatchStateRequest(), timeout=timeout_s),
            lambda frame: convert.rig_state_from_wire(frame.state),
        )

    # -- the trace --------------------------------------------------------------

    def read_trace(self, since_entry_number: int = 0, limit: int = 500) -> TraceWindow:
        """A window out of the trace ring. `State/ReadTrace`.

        Read `lost_entries_before` on what comes back: the ring is bounded, and
        a window that starts before the oldest entry it still holds is a window
        with a hole in it.
        """
        return convert.trace_window_from_wire(
            call(
                lambda: self._state.ReadTrace(
                    state_pb2.ReadTraceRequest(
                        since_entry_number=since_entry_number, limit=limit
                    )
                )
            )
        )

    def watch_trace(
        self,
        since_entry_number: int = 0,
        *,
        observer: str = DEFAULT_OBSERVER_NAME,
        timeout_s: float | None = None,
    ) -> DaemonStreamSubscription[TraceEntry]:
        """Trace entries as they are recorded. `State/WatchTrace`.

        **The backlog comes first.** `since_entry_number` is where to start,
        and 0 means everything the ring still holds — so a subscriber that
        reconnects picks up where it left off rather than losing whatever
        happened while it was away. Pass
        `read_state().newest_trace_entry_number + 1` to see only what happens
        from now on.

        The stream does not announce a gap. If the ring wraps past where you
        are, the next entry's `entry_number` is not the one you expected;
        compare them, and recover the trial with :meth:`read_trial_trace`.

        `observer` is the name this subscription answers to in
        :meth:`read_observers`. Name it: an unnamed observer on a rig with
        three scripts running is a question nobody can answer.
        """
        return DaemonStreamSubscription(
            lambda: self._state.WatchTrace(
                state_pb2.WatchTraceRequest(since_entry_number=since_entry_number),
                metadata=(("observer-name", observer),),
                timeout=timeout_s,
            ),
            convert.trace_entry_from_wire,
        )

    def wait_for_trial(
        self, trial_id: int, *, timeout_s: float, since_entry_number: int = 0
    ) -> TraceEntry:
        """Block until that trial ends, and give the entry that says so.

        **The loop every caller of this API writes**, which is why it is here
        rather than in each of them: subscribe, read until the result for one
        trial goes past, stop. Getting it wrong quietly — no deadline, or the
        wrong kind — is a script that hangs a rig at three in the morning.

        `timeout_s` is required and has no default. Only the side that knows a
        trial is in flight can tell "not yet" from "never", and that is the
        caller; a client that picked a number would be guessing at somebody
        else's experiment.

        **Subscribe before you arm.** Call this after `start_trial` and the
        backlog covers you — `since_entry_number=0` carries everything the ring
        still holds — but only for as long as the ring holds it. On a busy rig,
        read `newest_trace_entry_number` before arming and pass it here.

        Raises:
            DaemonRefusedTheRequest: with `status` `deadline_exceeded` if the
                trial has not ended in `timeout_s`. That is an answer, not a
                failure of this call: the trial is still running, or it ended
                in a way that published nothing, and both are worth knowing.
        """
        with self.watch_trace(since_entry_number, timeout_s=timeout_s) as entries:
            for entry in entries:
                if entry.kind == KIND_TRIAL_RESULT and entry.trial_id == trial_id:
                    return entry
        raise DaemonRefusedTheRequest(
            "deadline_exceeded",
            "no_result_yet",
            f"trial {trial_id} did not end within {timeout_s:g}s",
            "trial_id",
        )

    def read_trial_trace(self, trial_id: int) -> list[TraceEntry]:
        """Every entry the ring still holds for one trial. `State/ReadTrialTrace`.

        **This is what makes a lost subscription recoverable.** It answers
        exactly whatever the stream did, so a consumer that saw
        `lost_entries_before` can ask for the trial and get the whole of it —
        as long as the ring has not wrapped past it.
        """
        answer = call(
            lambda: self._state.ReadTrialTrace(
                state_pb2.ReadTrialTraceRequest(trial_id=trial_id)
            )
        )
        return [convert.trace_entry_from_wire(entry) for entry in answer.entries]

    def read_observers(self) -> Observers:
        """Everything watching this daemon right now. `State/ReadObservers`."""
        return convert.observers_from_wire(
            call(lambda: self._state.ReadObservers(service_pb2.ReadObserversRequest()))
        )

    # -- trials -----------------------------------------------------------------

    def configure_trial(
        self,
        trial_id: int,
        *,
        graph: str = "",
        cap_milliseconds: int = 0,
        start_source: str = "",
        start_line: int | None = None,
        distribution_patches: list[DistributionPatch] | None = None,
    ) -> ConfigureTrialResult:
        """Arm one trial on the board. `Trial/Configure`.

        `graph` defaults to the session's active graph. `cap_milliseconds` is
        enforced **by the board**, not by this host and not by the daemon,
        which is the whole reason the state machine runs there.

        `start_source` is what starts it — a host `start` call, or a line going
        active, in which case `start_line` names it.

        This is the call that fails when the link is slow: the board has to
        acknowledge it, and until it does the trial is not armed. A
        `DaemonIsUnavailable` here means exactly that.
        """
        request = trial_pb2.ConfigureTrialRequest(
            trial_id=trial_id,
            graph=graph,
            cap_milliseconds=cap_milliseconds,
            start_source=start_source,
            distribution_patches=[
                convert.distribution_patch_to_wire(patch)
                for patch in (distribution_patches or ())
            ],
        )
        if start_line is not None:
            request.start_line = start_line
        return convert.configure_result_from_wire(call(lambda: self._trial.Configure(request)))

    def start_trial(self, trial_id: int) -> StartTrialResult:
        """Start the armed trial. `Trial/Start`.

        The result carries the board's own clock reading, which is the one to
        align other recordings to.
        """
        return convert.start_result_from_wire(
            call(lambda: self._trial.Start(trial_pb2.StartTrialRequest(trial_id=trial_id)))
        )

    def cancel_trial(self, trial_id: int) -> CancelTrialResult:
        """Stop a trial in flight. `Trial/Cancel`.

        `cancelled` is `False` where it had already ended, which is not an
        error: the trial you wanted stopped is stopped either way.
        """
        return convert.cancel_result_from_wire(
            call(lambda: self._trial.Cancel(trial_pb2.CancelTrialRequest(trial_id=trial_id)))
        )

    def read_trial_result(self, trial_id: int | None = None) -> TrialResult:
        """A finished trial, with its path. `Trial/ReadResult`.

        With no `trial_id`, the most recent one. Read `path_was_truncated`: the
        board's path buffer is bounded and a graph that loops long enough fills
        it, and then `visits` is a prefix rather than the trial.
        """
        request = trial_pb2.ReadTrialResultRequest()
        if trial_id is not None:
            request.trial_id = trial_id
        return convert.trial_result_from_wire(call(lambda: self._trial.ReadResult(request)))

    # -- the board --------------------------------------------------------------

    def read_device(self) -> DeviceState:
        """The board, as the daemon last saw it. `Device/ReadDevice`."""
        return convert.device_state_from_wire(
            call(lambda: self._device.ReadDevice(service_pb2.ReadDeviceRequest()))
        )

    def open_link(self) -> DeviceState:
        """Open the serial link, or reopen it. `Device/OpenLink`.

        Not "connect the board": the port is in the rig config, and this opens
        whatever is there. A daemon started with `connect_on_startup` has done
        this already.
        """
        return convert.device_state_from_wire(
            call(lambda: self._device.OpenLink(service_pb2.OpenLinkRequest()))
        )

    def read_lines(self) -> LineMapView:
        """The wiring: named lines, and the pins the board says it has.
        `Device/ReadLines`."""
        return convert.line_map_from_wire(
            call(lambda: self._device.ReadLines(service_pb2.ReadLinesRequest()))
        )

    def write_line_map(self, name: str, text: str) -> WriteLineMapResult:
        """Write a line map, push it to the board and save it into the config.
        `Device/WriteLineMapFile`.

        `text` is the document as JSON — the daemon is what validates it, and
        it is refused **before anything is kept**, so a rig that says no is
        still running on the wiring it had.

        Three separate facts come back, because they come apart: saved without
        pushed (no board), pushed without saved (no config loaded), or both.
        """
        return convert.write_line_map_result_from_wire(
            call(
                lambda: self._device.WriteLineMapFile(
                    documents_pb2.StoredFile(name=name, text=text)
                )
            )
        )

    def read_serial_monitor(
        self, since_entry_number: int = 0, limit: int = 500
    ) -> SerialMonitorWindow:
        """Lines of text off the serial port. `Device/ReadSerialMonitor`.

        A debugging view of the wire itself, not a data path — the trace is
        where trials are recorded.
        """
        return convert.serial_monitor_window_from_wire(
            call(
                lambda: self._device.ReadSerialMonitor(
                    device_pb2.ReadSerialMonitorRequest(
                        since_entry_number=since_entry_number, limit=limit
                    )
                )
            )
        )

    def watch_serial_monitor(
        self, since_entry_number: int = 0, *, timeout_s: float | None = None
    ) -> DaemonStreamSubscription[SerialMonitorEntry]:
        """The serial port's text, as it goes past. `Device/WatchSerialMonitor`.

        The ring is bounded here too, and the stream does not announce a gap:
        compare `entry_number` against the one you expected.
        """
        return DaemonStreamSubscription(
            lambda: self._device.WatchSerialMonitor(
                device_pb2.WatchSerialMonitorRequest(since_entry_number=since_entry_number),
                timeout=timeout_s,
            ),
            convert.serial_monitor_entry_from_wire,
        )

    def read_firmware(self) -> FirmwareVersions:
        """What is running on the board, against what this host has.
        `Device/ReadFirmware`."""
        return convert.firmware_from_wire(
            call(lambda: self._device.ReadFirmware(service_pb2.ReadFirmwareRequest()))
        )

    def read_autorun(self) -> Autorun:
        """The board's own trial loop, as configured. `Device/ReadAutorun`."""
        return convert.autorun_from_wire(
            call(lambda: self._device.ReadAutorun(service_pb2.ReadAutorunRequest()))
        )

    def write_autorun(
        self,
        enabled: bool,
        *,
        graph_name: str | None = None,
        cap_milliseconds: int = 0,
        seed: int | None = None,
        first_trial_id: int | None = None,
        start_now: bool | None = None,
    ) -> Autorun:
        """Turn the board's own trial loop on or off. `Device/WriteAutorun`.

        **This is the board running trials with no host in the loop**, which is
        what makes a rig survive a host that goes away. `seed` fixes the draw,
        so a session can be repeated; left unset the board picks one.

        The `None` defaults are "leave it": an autorun already configured keeps
        its graph when you enable it again.

        **`start_now=False` is how a rig is actually set up**: enable, save,
        power cycle. `save_settings` is refused on a board that is running, and
        a board arming its own trials is never idle — so without this the
        setting could never reach the flash.
        """
        request = device_pb2.WriteAutorunRequest(
            enabled=enabled, cap_milliseconds=cap_milliseconds
        )
        if graph_name is not None:
            request.graph_name = graph_name
        if seed is not None:
            request.seed = seed
        if first_trial_id is not None:
            request.first_trial_id = first_trial_id
        if start_now is not None:
            request.start_now = start_now
        return convert.autorun_from_wire(call(lambda: self._device.WriteAutorun(request)))

    def save_settings(self) -> SaveSettingsResult:
        """Persist the board's settings to its own flash. `Device/SaveSettings`.

        The board's, not the daemon's: wiring, graph set and autorun survive a
        power cycle only once this has been called.

        **Read `write_count`.** Data flash wears out — about 100,000 erase
        cycles on the reference board — and this is the only thing that says
        how far through that budget a rig is. `written` is `False` when the
        settings were already there, which is a success: the board compares
        before it writes, so pressing save twice costs nothing.

        Refused while a trial is running: it erases flash, and the board will
        not stall its scan loop for that.
        """
        return convert.save_settings_result_from_wire(
            call(lambda: self._device.SaveSettings(service_pb2.SaveSettingsRequest()))
        )

    # -- the graph store --------------------------------------------------------

    def list_graphs(self) -> list[GraphSummary]:
        """Every graph in the store. `GraphStore/ListGraphs`.

        A file that will not parse is listed with `readable=False` and the
        reason in `detail`, rather than making the whole listing fail.
        """
        answer = call(lambda: self._graphs.ListGraphs(service_pb2.ListGraphsRequest()))
        return [convert.graph_summary_from_wire(item) for item in answer.graphs]

    def read_graph(self, name: str) -> StoredFile:
        """One graph, as the text it is on disk. `GraphStore/ReadGraphFile`."""
        return convert.stored_file_from_wire(
            call(lambda: self._graphs.ReadGraphFile(documents_pb2.ReadFileRequest(name=name)))
        )

    def write_graph(self, name: str, text: str) -> GraphSummary:
        """Write one graph into the store. `GraphStore/WriteGraphFile`.

        The name in the document and the name it is filed under have to agree;
        the daemon refuses a mismatch rather than picking one.
        """
        return convert.graph_summary_from_wire(
            call(
                lambda: self._graphs.WriteGraphFile(
                    documents_pb2.StoredFile(name=name, text=text)
                )
            )
        )

    def delete_graph(self, name: str) -> list[GraphSummary]:
        """Remove a graph from the store. `GraphStore/DeleteGraph`.

        Refused while it is in the committed set: deleting a graph a session is
        running is a session that stops making sense halfway through.
        """
        answer = call(
            lambda: self._graphs.DeleteGraph(documents_pb2.DeleteFileRequest(name=name))
        )
        return [convert.graph_summary_from_wire(item) for item in answer.graphs]

    def validate_graph(self, name: str) -> GraphValidation:
        """Compile a stored graph against the attached board.
        `GraphStore/ValidateGraph`.

        "Will this fit" is a question about *this* board and cannot be answered
        without one.
        """
        return convert.graph_validation_from_wire(
            call(lambda: self._graphs.ValidateGraph(documents_pb2.ReadFileRequest(name=name)))
        )

    def validate_graph_text(self, text: str) -> GraphValidation:
        """The same, for a graph that is not in the store yet.
        `GraphStore/ValidateGraphFile`.

        What an editor calls while somebody is typing, so a graph can be
        checked before it is saved anywhere.
        """
        return convert.graph_validation_from_wire(
            call(lambda: self._graphs.ValidateGraphFile(documents_pb2.FileDraft(text=text)))
        )

    def upload_graph(self, name: str) -> CommittedGraphSet:
        """Push one stored graph to the board on its own. `GraphStore/UploadGraph`.

        A bench convenience, and it **replaces** whatever set was committed —
        the board holds one set, not a pile of graphs. A session uses
        :meth:`upload_graph_set`, which commits the whole set at one version:
        the version a trial is configured against.
        """
        return convert.committed_set_from_wire(
            call(lambda: self._graphs.UploadGraph(documents_pb2.ReadFileRequest(name=name)))
        )

    # -- the state machine config store -----------------------------------------

    def list_configs(self) -> StateMachineConfigSummaries:
        """Every state machine config in the store, and which is loaded.
        `StateMachineConfigStore/ListConfigs`.

        The **session's** document — not the rig config, which is TOML on the
        box and is :meth:`read_configuration`.
        """
        return convert.config_summaries_from_wire(
            call(lambda: self._configs.ListConfigs(service_pb2.ListConfigsRequest()))
        )

    def read_config(self, name: str) -> StoredFile:
        """One state machine config, as the JSON text it is on disk.
        `StateMachineConfigStore/ReadConfigFile`."""
        return convert.stored_file_from_wire(
            call(lambda: self._configs.ReadConfigFile(documents_pb2.ReadFileRequest(name=name)))
        )

    def write_config(self, name: str, text: str) -> StateMachineConfigSummaries:
        """Write one state machine config into the store.
        `StateMachineConfigStore/WriteConfigFile`.

        **Saving is not loading.** The rig goes on running whatever it had; the
        whole store comes back so `loaded` says whether this write landed on
        the config in use, which is the question somebody editing during a
        session is asking.
        """
        return convert.config_summaries_from_wire(
            call(
                lambda: self._configs.WriteConfigFile(
                    documents_pb2.StoredFile(name=name, text=text)
                )
            )
        )

    def delete_config(self, name: str) -> StateMachineConfigSummaries:
        """Remove a state machine config. `StateMachineConfigStore/DeleteConfig`."""
        return convert.config_summaries_from_wire(
            call(lambda: self._configs.DeleteConfig(documents_pb2.DeleteFileRequest(name=name)))
        )

    def load_config(self, name: str) -> LoadedConfigResult:
        """Load a state machine config: this is what starts a session's setup.
        `StateMachineConfigStore/LoadConfig`.

        It names the graphs and carries the wiring, which is pushed to the
        board here. With no board attached the config is still loaded and
        `wiring_pushed` is `False` — the daemon holds it and pushes when a
        board arrives.
        """
        return convert.loaded_config_result_from_wire(
            call(lambda: self._configs.LoadConfig(documents_pb2.ReadFileRequest(name=name)))
        )

    # -- the session ------------------------------------------------------------

    def read_session(self) -> SessionState:
        """The session, in one read. `Session/ReadSession`."""
        return convert.session_state_from_wire(
            call(lambda: self._session.ReadSession(service_pb2.ReadSessionRequest()))
        )

    def open_session(self) -> OpenSessionResult:
        """Commit the loaded config's graph set to the board. `Session/Open`.

        The set goes up at one version, and a trial is configured against that
        version — so a set replaced under a trial's feet is a trial the board
        refuses rather than one that runs the wrong graph.
        """
        return convert.open_session_result_from_wire(
            call(lambda: self._session.Open(service_pb2.OpenSessionRequest()))
        )

    def upload_graph_set(self, graph_names: list[str]) -> OpenSessionResult:
        """Commit a set named here rather than by the config. `Session/UploadGraphs`.

        What a bench script uses when there is no session document to load.
        """
        return convert.open_session_result_from_wire(
            call(
                lambda: self._session.UploadGraphs(
                    service_pb2.UploadGraphsRequest(graph_names=list(graph_names))
                )
            )
        )

    def close_session(self) -> CloseSessionResult:
        """End the session. `Session/Close`.

        **The board keeps its set**, which is what makes a reconnect cheap and
        what lets a session resume after a daemon restart. Closing is this
        daemon's own bookkeeping plus one act on the board: an armed trial is
        cancelled, because one with nobody driving it is a rig that will run
        one more trial whenever somebody next touches a lever.

        `cancelled_trial_id` says which trial that was. Read it — a caller that
        armed a trial and then closed has had that trial taken away.
        """
        return convert.close_session_result_from_wire(
            call(lambda: self._session.Close(service_pb2.CloseSessionRequest()))
        )

    def set_active_graph(self, graph: str) -> str:
        """Which graph a trial configured without one will use.
        `Session/SetActiveGraph`."""
        answer = call(
            lambda: self._session.SetActiveGraph(session_pb2.SetActiveGraphRequest(graph=graph))
        )
        return answer.active_graph

    def clear_active_graph(self) -> str:
        """Unset it, so every trial has to name its graph.
        `Session/ClearActiveGraph`."""
        answer = call(
            lambda: self._session.ClearActiveGraph(service_pb2.ClearActiveGraphRequest())
        )
        return answer.active_graph

    # -- recordings -------------------------------------------------------------

    def read_recordings(self) -> Recordings:
        """The recording store, and the one being written to.
        `Recording/ReadRecordings`."""
        return convert.recordings_from_wire(
            call(lambda: self._recording.ReadRecordings(service_pb2.ReadRecordingsRequest()))
        )

    def start_recording(self, name: str = "", description: str = "") -> RecordingManifest:
        """Begin keeping trace entries under a name. `Recording/Start`.

        **A recording is taken off the ring rather than instead of it**: the
        ring keeps running, and this is what stops entries falling out of the
        record. With no `name` the daemon picks one from the clock.
        """
        return convert.recording_manifest_from_wire(
            call(
                lambda: self._recording.Start(
                    recording_pb2.StartRecordingRequest(name=name, description=description)
                )
            )
        )

    def pause_recording(self) -> RecordingManifest:
        """Stop keeping entries, without closing the recording. `Recording/Pause`.

        The gap is kept as a gap: a paused recording has segments, and the hole
        between them is a fact about the session rather than something to
        smooth over.
        """
        return convert.recording_manifest_from_wire(
            call(lambda: self._recording.Pause(service_pb2.PauseRecordingRequest()))
        )

    def resume_recording(self) -> RecordingManifest:
        """Start a new segment on the open recording. `Recording/Resume`."""
        return convert.recording_manifest_from_wire(
            call(lambda: self._recording.Resume(service_pb2.ResumeRecordingRequest()))
        )

    def stop_recording(self) -> RecordingManifest:
        """Close the recording and write it out. `Recording/Stop`."""
        return convert.recording_manifest_from_wire(
            call(lambda: self._recording.Stop(service_pb2.StopRecordingRequest()))
        )

    def clear_recording(self) -> RecordingManifest:
        """Throw the open recording away. `Recording/Clear`.

        For the run that was a mistake. A closed recording is deleted with
        :meth:`delete_recording`.
        """
        return convert.recording_manifest_from_wire(
            call(lambda: self._recording.Clear(service_pb2.ClearRecordingRequest()))
        )

    def read_recording(self, name: str) -> RecordingManifest:
        """One recording's manifest. `Recording/ReadRecording`.

        `kind_counts` is enough to know whether it is the one you want without
        reading the entries.
        """
        return convert.recording_manifest_from_wire(
            call(lambda: self._recording.ReadRecording(recording_pb2.RecordingName(name=name)))
        )

    def read_recording_entries(
        self, name: str, offset: int = 0, limit: int = 500
    ) -> RecordingEntries:
        """A page of one recording's entries. `Recording/ReadEntries`."""
        return convert.recording_entries_from_wire(
            call(
                lambda: self._recording.ReadEntries(
                    recording_pb2.ReadRecordingEntriesRequest(
                        name=name, offset=offset, limit=limit
                    )
                )
            )
        )

    def delete_recording(self, name: str) -> Recordings:
        """Remove a closed recording from the store. `Recording/DeleteRecording`."""
        return convert.recordings_from_wire(
            call(
                lambda: self._recording.DeleteRecording(recording_pb2.RecordingName(name=name))
            )
        )

    # -- the rig configuration --------------------------------------------------

    def read_configuration(self) -> RigConfiguration:
        """The **rig config**: this box's hardware. `Configuration/ReadConfiguration`.

        Not the session's document — that is :meth:`list_configs` and
        :meth:`load_config`. The two are never the same file
        (`contracts/DAEMON_LAYOUT.md` §1).
        """
        return convert.rig_configuration_from_wire(
            call(
                lambda: self._configuration.ReadConfiguration(
                    service_pb2.ReadConfigurationRequest()
                )
            )
        )

    def patch_configuration(self, patch: RigConfigurationPatch) -> RigConfigurationUpdate:
        """Change some of the rig config. `Configuration/PatchConfiguration`.

        A patch and not a replace, and only six fields: the directories are
        where a running daemon's files *are*, and changing one over the network
        would move a store out from under an open session. Those are edited on
        the box.

        Changing `device_target` tears the link down and brings it back, which
        `reconnected` reports.
        """
        return convert.rig_configuration_update_from_wire(
            call(
                lambda: self._configuration.PatchConfiguration(
                    convert.rig_configuration_patch_to_wire(patch)
                )
            )
        )


def _target(address: str, port: int) -> str:
    """`host`, `host:port` or nothing, as a gRPC target.

    A bare host gets the default port appended. An address that already names
    one is left alone, which is what makes `port=` and `host:port` two ways of
    saying the same thing rather than two settings that can disagree.
    """
    address = address.strip()
    if not address:
        return f"127.0.0.1:{port}"
    address = address.removeprefix("http://").removeprefix("https://").rstrip("/")
    if ":" in address.rsplit("]", 1)[-1]:
        return address
    return f"{address}:{port}"
