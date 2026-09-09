# SPDX-License-Identifier: LGPL-3.0-or-later
"""One rig, addressed by base URL.

`docs/reference/api.md` is the specification and this is the whole of it in Python: the
device and its wiring, the graph store, a session's graph set, the trial loop,
the trace and its subscription, the two configurations, and the recordings.

**The shape follows the API's own split**, because that split is a statement
about authority rather than an arrangement of routes:

`trial`, `session` and `trace` are the trial loop: small, stable, in the
critical path of every trial, and the whole of what a decision authority needs.
`device`, `graphs`, `configs`, `rig` and `recordings` are everything else --
what board is attached, which pin is the left lever, what this rig is wired
like. The half that exists for a person.

A caller that only drives trials touches three of those and never learns that
the rest is here, which is the same reason `triald.executor.TrialExecutor` is
three methods wide: every rig-configuring call that leaked into a trial loop
would be a way for the decision authority to become the thing that configures
the rig.

**Payloads are dictionaries here, and typed models are one import away.** These
methods take and return plain JSON, so reading a document, editing it and
writing it back works with no translation -- which is what `PATCH
/api/device/lines` and `PUT /api/state-machine-configs/{name}` were designed
for. When you want a graph checked *before* the round trip, `statemachined.model`
holds the same pydantic models the daemon validates with, in the same package:

```python
from statemachined.model.graph_definition import GraphDefinition

graph = GraphDefinition.model_validate(authored)   # refused here, naming the field
rig.graphs.write(graph.model_dump())
```

One definition, not two. That is the whole reason the client and the daemon are
one distribution rather than a package and a transcription of it.
"""

from __future__ import annotations

from typing import Any

import httpx

from ._transport import (
    DEFAULT_TIMEOUT_SECONDS,
    UPLOAD_TIMEOUT_SECONDS,
    Transport,
    websocket_url_for,
)
from .trace import DEFAULT_OBSERVER_NAME, TraceSubscription

#: A list of the daemon's own documents. Named at module scope rather than
#: written inline, because a method called `list` shadows the builtin inside its
#: own class body and a type checker then reads `list[...]` as a subscript of
#: the method.
Documents = list[dict[str, Any]]

#: Where a statemachined serves unless it was told otherwise. The same default
#: `statemachined serve` and `make bench` use.
DEFAULT_BASE_URL = "http://127.0.0.1:8081"


class StatemachinedClient:
    """A statemachined daemon, and everything it will do for you.

    ```python
    with StatemachinedClient("http://rig-3.local:8081") as rig:
        rig.session.upload_graph_set(["go-nogo", "2afc"])
        rig.trial.configure(trial_id=1, graph="go-nogo", cap_milliseconds=30_000)
        rig.trial.start(1)
    ```
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
        open_websocket=None,
    ) -> None:
        """
        Args:
            base_url: `http://host:port`. No trailing slash is required.
            timeout_seconds: the deadline on an ordinary call. The two uploads
                get their own, longer one -- see `UPLOAD_TIMEOUT_SECONDS`.
            http_client: what carries the requests. The default opens one on the
                network; passing `httpx.Client(transport=httpx.ASGITransport(app))`
                points this at a daemon in the same process, which is how the
                tests here exercise the real far end rather than a mock of it.
                A client passed in is never closed by this object.
            open_websocket: a callable taking a `ws://` URL and returning
                something with `recv()` and `close()`. The default uses
                `websockets`; injected, it lets a caller subscribe over another
                library, or over a test client, without monkeypatching.
        """
        self._transport = Transport(base_url, timeout_seconds, http_client)
        self._open_websocket = open_websocket or _connect_with_websockets

        self.device = DeviceApi(self._transport, self._open_websocket)
        self.graphs = GraphApi(self._transport)
        self.session = SessionApi(self._transport)
        self.configs = StateMachineConfigApi(self._transport)
        self.rig = RigConfigApi(self._transport)
        self.trial = TrialApi(self._transport)
        self.trace = TraceApi(self._transport, self._open_websocket)
        self.recordings = RecordingApi(self._transport)

    @property
    def base_url(self) -> str:
        return self._transport.base_url

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> StatemachinedClient:
        return self

    def __exit__(self, *_exception) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.base_url!r})"

    # ------------------------------------------------------- the whole rig ---

    def health(self) -> dict[str, Any]:
        """Is the daemon up, and does it have a device.

        Two different questions, and systemd only asks the first: a daemon whose
        board is unplugged is still the thing you ask *why*. So this answers
        `{"ok": ..., "device_connected": ...}` and never raises for the second.
        """
        return self._transport.get("/api/health", doing="reading the rig's health")

    def state(self) -> dict[str, Any]:
        """One snapshot: the link, the armed trial, the state by name, the io words."""
        return self._transport.get("/api/state", doing="reading the rig's state")

    def state_stream_url(self) -> str:
        return websocket_url_for(self.base_url, "/api/stream")

    def subscribe_to_state(
        self, *, timeout_seconds: float | None = None
    ) -> TraceSubscription:
        """Watch the snapshot as it changes. **Coalesced** by the daemon.

        A client that falls behind gets the current state rather than a backlog
        of stale ones, which loses nothing: the latest snapshot is the whole
        truth. That is the opposite of `trace.subscribe`, and the difference is
        the only interesting thing about the pair -- coalescing a *trace* would
        lose records, which is the one thing it exists not to do.

        The same object carries both, so `.messages()` and `.entries()` work
        here too; `.finished_trials()` does not, because a state snapshot is not
        a trial ending.
        """
        return TraceSubscription(
            self.state_stream_url(),
            lambda: self._open_websocket(self.state_stream_url()),
            timeout_seconds,
        )


# =============================================================== the device ===


class DeviceApi:
    """What is attached, what it can hold, and which pin is the left lever.

    `docs/reference/api.md` §3, and the half of this API that is not in a trial's critical
    path. It is also the half that answers the question a rig raises at two in
    the morning -- is the valve wired to A0, and did it open -- which is why the
    daemon is a daemon and not a library.
    """

    def __init__(self, transport: Transport, open_websocket) -> None:
        self._transport = transport
        self._open_websocket = open_websocket

    def describe(self) -> dict[str, Any]:
        """Board, firmware, capabilities, the committed set, link and scan health.

        `capabilities` is the board's own answer and never an assumption: the
        reference board ships two images and a Teensy is a different set of
        numbers again. `scan` is the honest half of the timing claim -- a board
        that quietly misses scans looks exactly like one that is fine.
        """
        return self._transport.get("/api/device", doing="reading the device")

    def connect(self) -> dict[str, Any]:
        """Open the port, greet, and push this rig's wiring. Idempotent.

        **Greeting a board takes the rig.** A board that was arming its own
        trials stops doing so until it is told to again, which is deliberate: a
        daemon that crashed must not leave a board rewarding an animal nobody is
        watching.
        """
        return self._transport.post("/api/device/connect", doing="connecting to the device")

    def firmware(self) -> dict[str, Any]:
        """The version running, against what the installed package ships.

        The comparison is the point: a board in a rack cannot be asked which
        commit it is running.
        """
        return self._transport.get("/api/device/firmware", doing="reading firmware versions")

    # -------------------------------------------------------------- lines ---

    def lines(self) -> dict[str, Any]:
        """Every line by name, with its wiring and its level right now.

        `is_high_now` is the **only** way anything outside the device can check
        that a graph's line numbers reach the pins somebody wired: there is no
        read-back path from a pin, and the output word is the engine's own
        shadow rather than a measurement.

        The answer is shaped to be edited and handed back: `board_input_pins`
        and `board_output_pins` are the board's own labels indexed by line
        number and sit *beside* the two lists, because a `LineMap` refuses
        members it does not declare and `is_high_now` is the one exception.
        """
        return self._transport.get("/api/device/lines", doing="reading the device's lines")

    def set_lines(self, line_map: dict[str, Any]) -> dict[str, Any]:
        """Rename lines, and change the wiring. The body is a whole `LineMap`.

        **Renaming is free**: names are the daemon's alone and never reach the
        wire, so a rename changes no graph and needs no upload. The rest --
        invert, enable, debounce, output safe levels -- is pushed to the device
        in the same call, and is refused while a trial is armed or running.

        A line may name a pin (`{"name": "lever", "pin_label": "D6"}`) instead
        of an index, which is the form worth using: a bit position is not
        written anywhere on the hardware and `D6` is.

        This reaches the board and the loaded config *in memory*. It is not on
        disk until the config is saved -- the reply's `saved_to_the_store` says
        so, and it is always false here.
        """
        return self._transport.patch(
            "/api/device/lines", _without_live_levels(line_map), doing="changing the line map"
        )

    # ------------------------------------------------------------ autorun ---

    def autorun(self) -> dict[str, Any]:
        """Whether this board arms its own trials, and what it would run.

        Asked of the board rather than remembered by the daemon. `enabled` and
        `active` are **not the same fact**: `enabled` is the stored setting and
        survives a power cut; `active` is whether the board is driving trials
        right now, and a daemon that just greeted a self-driving board sees the
        first true and the second false.
        """
        return self._transport.get("/api/device/autorun", doing="reading the autorun setting")

    def set_autorun(
        self,
        enabled: bool,
        *,
        graph_name: str | None = None,
        cap_milliseconds: int = 0,
        seed: int | None = None,
        first_trial_id: int | None = None,
        start_now: bool = True,
    ) -> dict[str, Any]:
        """Hand the board the job of arming its own trials, or take it back.

        The switch that makes this daemon optional: the board starts each run
        itself and takes the interval between runs from the dwell the terminal
        state it reached declared. Pair it with :meth:`save` for a board that
        comes back from a power cut still doing it.

        Args:
            graph_name: a name, never a slot. Left out when enabling, the
                session's active graph is used.
            seed: the stream the board's own trials draw from. Left out,
                whatever the board holds stands -- which for a board restored
                from its own storage is the seed that makes the session replay.
            start_now: False records that this board should drive itself
                **without starting it**, which is how a rig is set up: a save is
                refused on a board that is running, and a board arming its own
                trials is never idle. So: enable, save, power cycle.

        Turning it *off* is never refused as busy: the run in flight ends
        through the ordinary exit path with its result reported, exactly as a
        cancel does.
        """
        body: dict[str, Any] = {
            "enabled": enabled,
            "cap_milliseconds": cap_milliseconds,
            "start_now": start_now,
        }
        if graph_name is not None:
            body["graph_name"] = graph_name
        if seed is not None:
            body["seed"] = seed
        if first_trial_id is not None:
            body["first_trial_id"] = first_trial_id
        return self._transport.put(
            "/api/device/autorun", body, doing="setting who arms the trials"
        )

    def save(self) -> dict[str, Any]:
        """Write the wiring, the graph set and the autorun settings to the board.

        All three then survive a power cut. No body: what is saved is what is
        there, because a save that took its own copy of the settings would be a
        second place for them to disagree.

        `write_count` in the reply is flash wear made visible -- the reference
        board's data flash is good for about 100,000 erase cycles -- and a save
        that would store what is already stored answers `"written": false` and
        costs no erase cycle at all.
        """
        return self._transport.post("/api/device/save", doing="saving settings to the board")

    # ----------------------------------------------------- the raw monitor ---

    def monitor(self, since_entry_number: int = 0, limit: int = 500) -> dict[str, Any]:
        """Every line in and out of the serial port, as it went, CRC included.

        Always recording, because the alternative is not: a fault that happens
        once an hour is not reproducible on demand. Nothing here interprets
        anything -- a line nothing could parse is in here too -- and a caller
        whose cursor has fallen out of the ring is told so in `lost_lines_before`
        rather than handed a shorter answer that looks complete.
        """
        return self._transport.get(
            "/api/device/monitor",
            doing="reading the serial monitor",
            params={"since_entry_number": since_entry_number, "limit": limit},
        )

    def monitor_stream_url(self) -> str:
        return websocket_url_for(self._transport.base_url, "/api/device/monitor/stream")

    def subscribe_to_monitor(
        self, *, timeout_seconds: float | None = None
    ) -> TraceSubscription:
        """Every line as it crosses the wire. Not coalesced, like the trace."""
        url = self.monitor_stream_url()
        return TraceSubscription(url, lambda: self._open_websocket(url), timeout_seconds)


# =============================================================== the graphs ===


class GraphApi:
    """The store, the validator, and trying one graph out.

    Every call names a graph by **name**. No caller ever sees or supplies a slot
    index -- the daemon built the set, so it is the only process that knows.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def list(self) -> Documents:
        """Every graph in the store, with its state count.

        A graph that no longer parses is listed as `{"readable": false}` with
        the reason rather than omitted: a paradigm that has silently vanished
        from a list is how somebody spends an afternoon looking for it.
        """
        return self._transport.get("/api/graphs", doing="listing the graph store")["graphs"]

    def read(self, name: str) -> dict[str, Any]:
        """One graph, in the authored form -- exactly what was stored."""
        return self._transport.get(f"/api/graphs/{name}", doing=f"reading graph {name!r}")

    def write(self, graph: dict[str, Any], name: str | None = None) -> dict[str, Any]:
        """Store one. Validated on the way in.

        The store therefore never holds a graph that could not be run, which is
        what lets everything downstream treat a stored name as a real paradigm.
        The name comes from the graph itself unless one is given, and the two
        must agree -- a graph stored under a name that is not its own is one the
        store refuses to load back.
        """
        name = name or graph["name"]
        return self._transport.put(
            f"/api/graphs/{name}", graph, doing=f"storing graph {name!r}"
        )

    def delete(self, name: str) -> dict[str, Any]:
        """Remove one. Refused while it is in the committed set."""
        return self._transport.delete(f"/api/graphs/{name}", doing=f"deleting graph {name!r}")

    def validate(self, name: str) -> dict[str, Any]:
        """Every rule, plus **this device's** caps. Changes nothing, uploads nothing.

        The capacity half is the useful half -- *"you have room for two more
        graphs"* is what somebody setting up a session wants to know -- and
        `warnings` is for a graph that is legal, uploads, runs, and is narrower
        than its author thinks.

        Needs a connected device: a graph checked against no board is a graph
        checked against nothing.
        """
        return self._transport.post(
            f"/api/graphs/{name}/validate", doing=f"validating graph {name!r}"
        )

    def upload(self, name: str) -> dict[str, Any]:
        """Compile and upload one graph **as a set of one**, and commit it.

        The bench path: trying a graph out. It replaces whatever set is
        committed and is therefore refused while a session's set is in place --
        losing a session's paradigms because somebody previewed a graph is not a
        recoverable mistake. Use :meth:`SessionApi.upload_graph_set` for a
        session.
        """
        return self._transport.post(
            f"/api/graphs/{name}/upload",
            doing=f"uploading graph {name!r} as a set of one",
            timeout_seconds=UPLOAD_TIMEOUT_SECONDS,
        )


# ============================================================== the session ===


class SessionApi:
    """Opening a session: the graphs it will use, on the device, once.

    After that a trial names one of them and arms in milliseconds, which is the
    whole reason the upload happens here rather than per trial.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def read(self) -> dict[str, Any]:
        """What this rig is loaded with, and whether a session is open.

        Three separate facts reported separately rather than collapsed into one
        "ready" flag, because the useful question is *which* of them is missing:
        a config may be loaded with no session open, and a set may still be
        committed on the board from a session that ended -- which is normal, and
        is what makes a reconnect cheap.
        """
        return self._transport.get("/api/session", doing="reading the session")

    def upload_graph_set(self, graph_names: list[str]) -> dict[str, Any]:
        """Compile, check and upload every graph a session will use.

        **Where a session is allowed to fail, and that is the point.** The
        daemon compiles every name, checks the *summed* pool usage against this
        board's caps, uploads the set and commits it -- before an animal is in
        the booth. The alternative is discovering at trial 40 that one trial
        type names a graph with forty states on a thirty-two-state board.

        The order of the names becomes their slots. It is the slowest call in
        this API by a wide margin -- tens of seconds on a UART rig -- so it gets
        its own timeout, and it is the only place a graph is uploaded during a
        session.

        A failed upload leaves the board holding **nothing**: two sets do not fit
        in 32 KB, so the device fills the live one. No trial can be armed until
        one uploads successfully, and every output sits at its safe level
        meanwhile.
        """
        return self._transport.post(
            "/api/session/graphs",
            {"graph_names": list(graph_names)},
            doing="uploading the session's graph set",
            timeout_seconds=UPLOAD_TIMEOUT_SECONDS,
        )

    def open(self) -> dict[str, Any]:
        """Put the *loaded config's* graphs on the device.

        The same upload as :meth:`upload_graph_set`, over the graphs a
        state-machine config names rather than a list a caller supplies. This is
        the one to use where there is a config, because then the line map and
        the graphs came from one file that was saved together; triald, which
        holds no config and knows only names, uses the other.
        """
        return self._transport.post(
            "/api/session/open",
            doing="opening the session",
            timeout_seconds=UPLOAD_TIMEOUT_SECONDS,
        )

    def close(self) -> dict[str, Any]:
        """End the session. Cancels an armed trial; leaves the set on the board.

        The set surviving is what makes a reconnect cheap, and unloading it
        would buy nothing but a slow start next time.
        """
        return self._transport.post("/api/session/close", doing="closing the session")

    def set_active_graph(self, graph: str) -> dict[str, Any]:
        """Which graph a trial gets **when it does not name one**.

        A default, not a mode: a trial that names a graph always wins, so a
        person switching graphs in a browser cannot change what a driven rig is
        running -- and a decision authority, which names one every trial, has no
        reason to know this exists.

        Nothing is pushed and no board is touched: the set is already committed
        and a graph is switched by index at configure time.
        """
        return self._transport.put(
            "/api/session/active-graph", {"graph": graph}, doing=f"selecting graph {graph!r}"
        )

    def clear_active_graph(self) -> dict[str, Any]:
        """Select nothing. A trial must then name its own graph."""
        return self._transport.delete(
            "/api/session/active-graph", doing="clearing the active graph"
        )


# ============================================================ the trial loop ===


class TrialApi:
    """Three calls out, and they are small on purpose.

    Everything that could have been done once per session was done once per
    session, in `session.upload_graph_set`. What is left here is what has to
    happen per trial, and it is the part a rig runs a thousand times a day.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def configure(
        self,
        trial_id: int,
        *,
        graph: str = "",
        cap_milliseconds: int = 0,
        start_source: str = "serial",
        distribution_patches: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Arm the device for exactly one trial, and do not return until it is.

        **No trial may start that the device was not confirmed configured
        for.** Skipping this is how a session runs the previous trial's
        parameters without anybody noticing.

        Args:
            trial_id: monotonic within the session, and on every message about
                this trial. A result for any other is refused rather than
                accepted -- which is what stops a late outcome being attributed
                to the trial after it.
            graph: a name. Omitted, the trial gets the rig's active graph, which
                is what a person pressing a button on a bench means and what a
                trial loop never relies on. A name the store does not hold, or
                one outside the committed set, is refused -- and the refusal
                says which name and which set.
            cap_milliseconds: a wall-clock ceiling on the whole trial. It stays
                regardless of the graph, because validation cannot tell a
                ten-second foreperiod from a hang.
            distribution_patches: per-trial overrides of named distributions,
                `{"name": "foreperiod", "minimum_ms": 250, "maximum_ms": 900}`,
                reverted when the trial ends. They cannot change the *shape* of
                anything -- that would be a different graph.

        **Nothing is uploaded here.** `elapsed_milliseconds` is reported anyway,
        so an arm that took longer than it should is a number per trial rather
        than an inference from a trial that started late.
        """
        body: dict[str, Any] = {
            "trial_id": trial_id,
            "graph": graph,
            "cap_milliseconds": cap_milliseconds,
            "start_source": start_source,
        }
        if distribution_patches:
            body["distribution_patches"] = list(distribution_patches)
        return self._transport.post(
            "/api/trial/configure", body, doing=f"arming trial {trial_id}"
        )

    def start(self, trial_id: int) -> dict[str, Any]:
        """Begin the trial the device was armed for.

        Refused unless the device is armed for that id and the configured
        `start_source` admits serial.
        """
        return self._transport.post(
            "/api/trial/start", {"trial_id": trial_id}, doing=f"starting trial {trial_id}"
        )

    def cancel(self, trial_id: int) -> dict[str, Any]:
        """Abandon the trial in flight, and hear what actually happened.

        A cancel that races a terminal state comes back with the **real**
        outcome, not a fabricated one -- asking to cancel and being told `HIT` is
        the caller's to cope with, and the alternative is a record claiming a
        trial was cancelled when the animal had already responded.

        The cancel itself is a forced transition through the ordinary exit path,
        so every output the state raised is lowered by the same code that lowers
        it on any other transition. A valve cannot be left open by this.
        """
        return self._transport.post(
            "/api/trial/cancel", {"trial_id": trial_id}, doing=f"cancelling trial {trial_id}"
        )

    def result(self) -> dict[str, Any]:
        """The last completed trial, read back into names.

        Named against the graph that **actually ran** -- the compiled set the
        daemon uploaded -- rather than against whatever the store holds today,
        which is what keeps a renamed state from mislabelling last week's data.

        This is the *last* trial and not a trial by id: use
        `trace.for_trial(id)` for that, which answers exactly and is what makes
        a dropped notification recoverable rather than fatal.
        """
        return self._transport.get("/api/trial/result", doing="reading the last trial result")


# ================================================================ the trace ===


class TraceApi:
    """A timestamped log of everything the machine did, kept whether or not
    anybody asked.

    One entry per state visit, plus the daemon's own events -- `configure`,
    `start`, `cancel`, link loss, an upload and what it cost, a save. That
    second half is there because aligning an external signal to trial 193 needs
    to know when trial 193 was *armed*, not only which states it visited.

    **This is not a `.tdr` and must not grow into one.** It joins to one on
    `trial_id`, which is the entire reason `trial_id` is on the wire.
    """

    def __init__(self, transport: Transport, open_websocket) -> None:
        self._transport = transport
        self._open_websocket = open_websocket

    def read(self, since_entry_number: int = 0, limit: int = 500) -> dict[str, Any]:
        """The ring, oldest first, from a cursor.

        `entry_number` is the daemon's own and is what `since_` means: the
        device's `seq` counts visits within a *run* and restarts at zero every
        trial, so it cannot address a position in a log that spans a session.
        Both are carried on every entry.

        A cursor that has fallen out of the ring is reported in
        `lost_entries_before` rather than silently satisfied with less.
        """
        return self._transport.get(
            "/api/trace",
            doing="reading the trace",
            params={"since_entry_number": since_entry_number, "limit": limit},
        )

    def for_trial(self, trial_id: int) -> list[dict[str, Any]]:
        """Everything the daemon published about one trial, by its id.

        **A pull, by id, and the reason a dropped notification is recoverable
        rather than fatal**: whatever the stream did, this answers exactly.

        Raises:
            NotFound: the ring holds nothing for that trial. Which is a real
                answer -- either it never ran, or it has aged out.
        """
        return self._transport.get(
            f"/api/trace/trial/{trial_id}", doing=f"reading the trace for trial {trial_id}"
        )["entries"]

    def observers(self) -> dict[str, Any]:
        """Who is reading a stream right now. A debugging aid and nothing else.

        The daemon never acts on this list. It exists for the question that is
        otherwise a packet capture: when trials stop reaching a consumer, is
        nothing connected, or is something connected and receiving nothing?
        """
        return self._transport.get("/api/observers", doing="reading the observer list")

    def stream_url(self, observer: str | None = DEFAULT_OBSERVER_NAME) -> str:
        """Where to watch. `?observer=` is a label and grants nothing."""
        return websocket_url_for(
            self._transport.base_url, "/api/trace/stream", observer=observer
        )

    def subscribe(
        self,
        observer: str | None = DEFAULT_OBSERVER_NAME,
        *,
        timeout_seconds: float | None = None,
    ) -> TraceSubscription:
        """Open a subscription. Opening it is the whole of subscribing.

        Nothing is registered on the far end, nothing waits for you, and closing
        the socket is the whole of leaving. The stream is **not** coalesced, and
        a subscriber too slow for the ring is told the range it lost -- as
        :class:`~statemachined.client.errors.TraceStreamLost` -- rather than
        handed a shorter answer that looks complete.

        ```python
        with rig.trace.subscribe("triald") as stream:
            rig.trial.configure(193, graph="go-nogo")
            rig.trial.start(193)
            if stream.wait_for_trial(193, timeout_seconds=35):
                report = rig.trace.for_trial(193)
        ```

        Note the order: subscribe *before* arming. A subscription opened after
        the trial started begins at the newest entry and can miss the result.
        """
        url = self.stream_url(observer)
        return TraceSubscription(url, lambda: self._open_websocket(url), timeout_seconds)


# ======================================================== the configurations ===


class RigConfigApi:
    """What the box is: the device, the directories, the timeouts.

    Backed by `/etc/braemons/statemachined-rig-config.toml`, which is a package
    conffile the daemon **never writes**. So a change made here lasts until the
    daemon restarts and then the file wins -- the reply says `until_restart`,
    and that is the honest behaviour for a conffile rather than a limitation.

    Not the line map and not the graphs: those are a state-machine config, they
    live under `/var/lib`, and they are what a person edits on a Tuesday.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def read(self) -> dict[str, Any]:
        return self._transport.get("/api/config", doing="reading the rig config")

    def replace(self, configuration: dict[str, Any]) -> dict[str, Any]:
        """Send a whole rig config. Refused while a trial is armed or running.

        A target that changed means a different device, and swapping the device
        under a running trial would move the thing the trial is measured
        against. A changed target or expected board reconnects.
        """
        return self._transport.patch(
            "/api/config", configuration, doing="changing the rig config"
        )

    def update(self, **changes: Any) -> dict[str, Any]:
        """Change some settings, keeping the rest.

        `PATCH /api/config` takes a whole configuration -- the daemon validates
        it as one object -- so this reads, merges and sends. Which means it is
        not atomic against another writer, and on a rig there is no other
        writer: the file is a conffile and the UI is the only other client.
        """
        return self.replace(self.read() | changes)


class StateMachineConfigApi:
    """What the box is doing today: the line map, and the graphs.

    Saved under `/var/lib/braemons/statemachined/configs/`, written by the web
    UI, and the half of the configuration a person owns.

    **Saving and loading are different verbs.** :meth:`write` puts a config in
    the store and touches no hardware; :meth:`load` makes one the rig's. A UI
    that could only save by also arming the rig is a UI nobody edits during a
    session.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def list(self) -> dict[str, Any]:
        """Every saved config as a summary, plus `loaded`, the one running.

        Summaries rather than whole configs, because a config carries its
        graphs. One that will not parse is listed with the reason instead of
        omitted -- a file you cannot see is a file you cannot fix.
        """
        return self._transport.get(
            "/api/state-machine-configs", doing="listing the state-machine configs"
        )

    def read(self, name: str) -> dict[str, Any]:
        return self._transport.get(
            f"/api/state-machine-configs/{name}", doing=f"reading config {name!r}"
        )

    def write(self, config: dict[str, Any], name: str | None = None) -> dict[str, Any]:
        """Save one. Does not load it, and does not touch the device.

        The reply's `is_the_loaded_config` says whether the rig is now running
        what was written -- it is not, until somebody loads it -- because a UI
        that did not say so would leave a person believing a wiring change had
        reached the board.
        """
        name = name or config["name"]
        return self._transport.put(
            f"/api/state-machine-configs/{name}", config, doing=f"saving config {name!r}"
        )

    def delete(self, name: str) -> dict[str, Any]:
        """Remove one from the store.

        A loaded config that is deleted stays loaded and the rig goes on running
        it: deleting a file is not a request to stop an experiment. `session.read()`
        then reports `is_still_in_the_store: false`.
        """
        return self._transport.delete(
            f"/api/state-machine-configs/{name}", doing=f"deleting config {name!r}"
        )

    def load(self, name: str) -> dict[str, Any]:
        """Make one the rig's: resolve its line map against the board, and push.

        Refused `422` if this board does not have those pins -- **before
        anything is kept**, so the rig carries on with the map it had -- and
        `409` while a trial is armed or running, because a line map is what the
        trial's own record means and moving it mid-trial makes that record a
        fiction.

        The graphs are *not* uploaded here. Loading says what this rig **is**;
        `session.open()` is what puts it on the device, and keeping them apart
        is what lets somebody load a config to look at it without disturbing a
        board.
        """
        return self._transport.post(
            f"/api/state-machine-configs/{name}/load", doing=f"loading config {name!r}"
        )


# =========================================================== the recordings ===


class RecordingApi:
    """The trace is always on. A recording is a name, a boundary somebody chose,
    and a file that is only this run.

    A sink on the trace rather than a poller of it, which means it sees every
    entry in order and none skipped. This is for the rig with **no** decision
    authority -- a bench, a pilot, a training box, where the daemon is the only
    thing that saw the session happen.

    **Pausing does not blind the rig.** The trace keeps running, so a pause
    leaves a gap that the manifest states, in the trace's own entry numbers. A
    recording that renumbered its entries would be claiming it saw everything,
    which is the one failure worse than not having recorded.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def list(self) -> dict[str, Any]:
        """Every recording on this rig, and which one is being written."""
        return self._transport.get("/api/recordings", doing="listing recordings")

    def start(self, name: str = "", description: str = "") -> dict[str, Any]:
        """Begin one. A blank name is the time it started, which still sorts."""
        return self._transport.post(
            "/api/recordings/start",
            {"name": name, "description": description},
            doing="starting a recording",
        )

    def pause(self) -> dict[str, Any]:
        """Stop capturing; keep the recording open. The gap is written down."""
        return self._transport.post("/api/recordings/pause", doing="pausing the recording")

    def resume(self) -> dict[str, Any]:
        """Capture again, into a new stretch."""
        return self._transport.post("/api/recordings/resume", doing="resuming the recording")

    def stop(self) -> dict[str, Any]:
        """End it. The file stays and the recording is in the store."""
        return self._transport.post("/api/recordings/stop", doing="stopping the recording")

    def clear(self) -> dict[str, Any]:
        """Throw away what is in it **and keep recording**.

        The verb that is not a stop: *"the last ten minutes were me testing a
        valve"* is a different thing from *"this recording is finished"*, and a
        UI with only stop would make somebody delete a file to say it.
        """
        return self._transport.post("/api/recordings/clear", doing="clearing the recording")

    def read(self, name: str) -> dict[str, Any]:
        """One recording's manifest: its state, its counts, and its segments."""
        return self._transport.get(
            f"/api/recordings/{name}", doing=f"reading recording {name!r}"
        )

    def entries(self, name: str, offset: int = 0, limit: int = 500) -> dict[str, Any]:
        """The entries, **by position in the file**.

        By position rather than by entry number, because a recording with a
        pause in it has no contiguous range of entry numbers -- the numbers are
        the trace's and the gap is real. Each entry still carries its
        `entry_number`, so a reader joins it back to the trace exactly.
        """
        return self._transport.get(
            f"/api/recordings/{name}/entries",
            doing=f"reading recording {name!r}",
            params={"offset": offset, "limit": limit},
        )

    def delete(self, name: str) -> dict[str, Any]:
        """Remove one. Refused while it is the one being written."""
        return self._transport.delete(
            f"/api/recordings/{name}", doing=f"deleting recording {name!r}"
        )


# ------------------------------------------------------------------ helpers ---


def _without_live_levels(line_map: dict[str, Any]) -> dict[str, Any]:
    """Strip what `GET /api/device/lines` added, so the answer can be sent back.

    `LineMap` forbids members it does not declare, and that read adds exactly
    one to each line -- `is_high_now`, a measurement rather than a setting -- as
    well as the board's own pin lists beside them. Dropping those here is what
    makes read-edit-write the obvious thing rather than a trap, and it is
    confined to the four names the API documents so that a genuine typo in a
    caller's map is still refused by the daemon, naming the field.
    """
    lines_only = {
        key: value
        for key, value in line_map.items()
        if key not in {"pin_labels_came_from", "board_input_pins", "board_output_pins"}
    }
    for side in ("input_lines", "output_lines"):
        if isinstance(lines_only.get(side), list):
            lines_only[side] = [
                {key: value for key, value in line.items() if key != "is_high_now"}
                if isinstance(line, dict)
                else line
                for line in lines_only[side]
            ]
    return lines_only


def _connect_with_websockets(url: str):
    """The default subscription transport.

    Imported here rather than at module scope so that importing this package
    costs nothing for a caller who only makes HTTP calls -- which is most of
    them, and all of the ones on a rig's critical path.

    `legacy=True` where the installed `websockets` has it. That library is
    changing what `connect()` returns: today a connection, in a future release a
    reconnecting object that must be used as a context manager. This package
    owns the lifetime itself -- :class:`TraceSubscription` is the context
    manager, and it is the one a caller sees -- so it asks for the connection
    explicitly rather than letting the default decide, which is what silences a
    deprecation warning that is about a decision made one layer up.
    """
    import inspect

    from websockets.sync.client import connect

    if "legacy" in inspect.signature(connect).parameters:
        return connect(url, legacy=True)
    return connect(url)
