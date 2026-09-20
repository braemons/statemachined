# SPDX-License-Identifier: LGPL-3.0-or-later
"""The types this client hands back, and the ones it takes.

**No protobuf type crosses this package's edge**, which is the rule
`contracts/DAEMON_LAYOUT.md` states for every client in this family: the
generated code is private, in `statemachined_client._proto`, and this module is
the vocabulary a person writes against. Somebody reading the duration of a
state visit should not have to learn a generated API to do it.

Everything here is a frozen dataclass, so a record you were handed cannot be
edited into something the daemon never said. `_wire_conversions.py` is the seam
and is the only module that imports the generated types.

**The names are the proto's**, field for field, because the family's rule is
that a name travels unchanged: `entered_device_microseconds` is
`entered_device_microseconds` in the proto, on the wire, in the trace line the
ring holds, in the browser and here.

**A quantity with a unit spells it.** `cap_milliseconds`, `drawn_duration_ms`,
`connected_seconds`, `entered_device_microseconds` — the daemon's rule, and a
number whose unit you have to look up is a number somebody will get wrong once.

Two deliberate departures from the wire, both in one direction and both handled
in one place:

* the enums. `statemachined.v1.TrialCancelReason` spells its values
  `TRIAL_CANCEL_REASON_LINK_LOST`, because protobuf requires a value name to be
  unique across every enum in its package. Nobody writing Python should have to
  say `TrialCancelReason.TRIAL_CANCEL_REASON_LINK_LOST`, so the prefix is
  dropped here and put back on the wire by the conversions.
* the host times. They cross as ISO-8601 strings, because that is what the
  trace ring writes and what a `.jsonl` line holds; they arrive here as
  `datetime`, because a client that hands back a string makes every caller
  parse it. The field name is unchanged, and an unparseable or empty one is
  `None` rather than an exception — a trace you can read with one odd timestamp
  in it beats a trace you cannot read at all.

**The documents are not here, and that is the design.** A graph, a line map and
a state machine config cross as *text*, in :class:`StoredFile`. They are files
somebody authored and the daemon is the only thing that validates one; a client
that carried its own copy of those models would carry a copy that is right
until it is not, and the day it is not is a session. Parse them with `json` if
you want to look inside.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field
from typing import Any

# -- the enums ------------------------------------------------------------------


class TrialOutcome(enum.IntEnum):
    """How a trial ended, as the `.tdr` code it has always been.

    An `IntEnum` because **these numbers are the contract**. They are in every
    `.tdr` the lab has written and in every analysis script that reads one, and
    they are never renumbered — so `TrialOutcome.HIT == 1` is a fact worth
    being able to rely on, and `int(outcome)` is what goes into a table.

    This is `braemons.v1.TrialOutcome`, shared with triald and with the board's
    own firmware, which is why it is the one enum here with no prefix to strip:
    a number that means `HIT` in three programs is spelled `HIT` in all three.
    """

    UNDETERMINED = -1
    NOT_STARTED = 0
    HIT = 1
    WRONG_RESPONSE = 2
    EARLY_HIT = 3
    EARLY_WRONG_RESPONSE = 4
    EARLY = 5
    LATE = 6
    EYE_ERROR = 7
    UNEXPECTED_START_SIGNAL = 8
    WRONG_START_SIGNAL = 9
    CANCELLED = 10
    NEVER_FINISHED = 11


class TrialCancelReason(enum.StrEnum):
    """Why a trial ended early, where it did.

    `NONE` on every trial that ran to a state with no transition out — which is
    most of them. A `StrEnum`, so a log line or a table cell can carry the word
    and compare equal to it.
    """

    #: Nothing cancelled it. The trial ended by reaching a terminal state.
    NONE = "none"
    #: A `cancel` call. Somebody, or a script, asked for it.
    HOST = "host"
    #: The serial link went away mid-trial. The board stops on its own.
    LINK_LOST = "link_lost"
    #: A line configured as the abort line went active.
    ABORT_LINE = "abort_line"
    #: `cap_milliseconds` elapsed. The board enforces this, not the daemon.
    TRIAL_TIMEOUT = "trial_timeout"


# -- the device -----------------------------------------------------------------


@dataclass(frozen=True)
class DeviceCapacities:
    """What this board can hold, as the board itself reports it.

    Read from the device at connect time and never guessed: two boards running
    the same firmware can have different limits, and a graph set is refused
    against these numbers *before* anything is pushed.
    """

    max_line: int = 0
    max_states: int = 0
    max_transitions: int = 0
    max_output_actions: int = 0
    max_distributions: int = 0
    max_choice_options: int = 0
    max_path: int = 0
    max_graphs: int = 0
    max_timers: int = 0
    #: The first line index that belongs to a timer rather than to a pin.
    first_timer_line: int = 0
    input_line_count: int = 0
    output_line_count: int = 0


@dataclass(frozen=True)
class CommittedGraphSet:
    """The graphs that are on the board right now.

    `set_version` increases every time a set is committed, and a trial is
    configured against a version: a set that was replaced under a trial's feet
    is a trial the board refuses rather than one that runs the wrong graph.
    """

    set_version: int = 0
    graph_names: list[str] = field(default_factory=list)
    #: How much of the board's compiled-graph pool this set uses, and how much
    #: there is. A set that does not fit is refused with both numbers.
    pool_usage: int = 0
    pool_capacity: int = 0


@dataclass(frozen=True)
class LinkHealth:
    """The serial link's counters, since the daemon connected.

    `dropped_lines` and `bad_lines` are the two that matter and they mean
    different things: dropped is the host not reading fast enough, bad is a
    line that failed its CRC. Neither is fatal and both are worth watching.
    """

    connection_count: int = 0
    dropped_lines: int = 0
    bad_lines: int = 0
    last_error: str = ""


@dataclass(frozen=True)
class ScanHealth:
    """How the board's scan loop is doing.

    `worst_gap` is the longest interval between scans seen since connect, in
    the same units the board reports. An overrun is a scan that started late
    enough to matter — which is the number to watch, because the whole point of
    running the state machine on a board is that it does not miss one.
    """

    hz: int = 0
    overruns: int = 0
    worst_gap: int = 0
    tx_stalls: int = 0


@dataclass(frozen=True)
class DeviceState:
    """The board, as the daemon last saw it.

    `connected` is the field to check first: every other one describes a board
    that may not be there. A daemon with no board is still answering — that is
    why asking it *why* works.
    """

    connected: bool = False
    #: The serial port, as configured. `/dev/ttyACM0`, or a simulator's name.
    target: str = ""
    board: str = ""
    firmware_version: str = ""
    protocol_version: int = 0
    measured_scan_hz: int = 0
    capacities: DeviceCapacities | None = None
    #: Whether a line map has been pushed. `None` where the daemon has not
    #: asked yet, which is not the same as "no".
    has_wiring: bool | None = None
    #: Where the pin labels came from — the board itself, or a config.
    pin_labels_came_from: str = ""
    committed_set: CommittedGraphSet | None = None
    link: LinkHealth | None = None
    scan: ScanHealth | None = None
    uptime_device_microseconds: int = 0


@dataclass(frozen=True)
class InputLine:
    """One input line, as the loaded config names it and the board sees it.

    `line_index` is `None` for a named line the config declares but this board
    has no pin for — which is a thing worth being able to see rather than a
    thing to refuse, because a config is written once and run on several rigs.
    """

    name: str = ""
    line_index: int | None = None
    pin_label: str = ""
    reads_active_low: bool = False
    is_enabled: bool = True
    #: The live level, where the daemon has one. `None` with no board attached.
    is_high_now: bool | None = None


@dataclass(frozen=True)
class OutputLine:
    """One output line.

    `safe_level_is_high` is the level the board drives when nothing is running,
    and it is per line because "off" is not the same voltage on every device.
    """

    name: str = ""
    line_index: int | None = None
    pin_label: str = ""
    safe_level_is_high: bool = False
    is_high_now: bool | None = None


@dataclass(frozen=True)
class LineMapView:
    """The wiring: named lines on one side, the board's own pins on the other.

    `board_input_pins` and `board_output_pins` are what the *board* says it
    has, so an editor can offer them rather than have somebody type a label
    that does not exist.
    """

    input_lines: list[InputLine] = field(default_factory=list)
    output_lines: list[OutputLine] = field(default_factory=list)
    pin_labels_came_from: str = ""
    board_input_pins: list[str] = field(default_factory=list)
    board_output_pins: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WriteLineMapResult:
    """What writing a line map actually did, in three separate facts.

    They come apart: a map can be saved and not pushed (no board), pushed and
    not saved (no config loaded to save it into), or both. One boolean would
    have had to pick which of those to lie about.
    """

    line_map: LineMapView | None = None
    pushed_to_device: bool = False
    saved_to_the_store: bool = False
    #: The config the map was saved into, where it was saved into one.
    state_machine_config: str = ""


@dataclass(frozen=True)
class SerialMonitorEntry:
    """One line of text on the serial port, in either direction.

    This is the wire itself — `direction` is `"rx"` or `"tx"` — and it is a
    debugging view, not a data path. The trace is where trials are recorded.
    """

    entry_number: int = 0
    direction: str = ""
    line: str = ""
    recorded_host_time: dt.datetime | None = None


@dataclass(frozen=True)
class SerialMonitorWindow:
    """A read out of the serial monitor's ring.

    `lost_entries_before` is set where the ring wrapped past what was asked
    for. It is `None` when nothing was lost, and that is the whole point of the
    field: a gap you are told about is recoverable and a gap you are not is a
    record that is quietly wrong.
    """

    entries: list[SerialMonitorEntry] = field(default_factory=list)
    newest_entry_number: int = 0
    oldest_entry_number_still_held: int = 0
    ring_capacity: int = 0
    lost_entries_before: int | None = None


@dataclass(frozen=True)
class FirmwareVersions:
    """What is running on the board, and what this host has to offer it.

    `comparable` is `False` where the two cannot be compared at all — an
    unstamped development build — and then `matches` means nothing. Two
    booleans rather than a tri-state because the question "are they the same"
    has three answers and one of them is "I cannot tell".
    """

    running: str = ""
    installed: str = ""
    running_is_stamped: bool = False
    comparable: bool = False
    matches: bool = False


@dataclass(frozen=True)
class Autorun:
    """The board running trials by itself, with no host in the loop.

    `enabled` is the setting and `active` is what is happening: a board can be
    configured for autorun and not running one, which is the state it is in
    between the setting and a session.
    """

    enabled: bool = False
    active: bool = False
    graph_name: str = ""
    slot: int = 0
    cap_milliseconds: int = 0
    seed: int = 0
    next_trial_id: int = 0


# -- the documents, as text -----------------------------------------------------


@dataclass(frozen=True)
class StoredFile:
    """A document in one of the daemon's stores, as the text it is on disk.

    Graphs and state machine configs both. **Not parsed here**: the daemon owns
    what a graph *is*, and a client with its own copy of those models has a
    copy that is right until it is not. `json.loads(file.text)` if you want to
    look inside; the daemon is what refuses a bad one, naming the field.
    """

    name: str = ""
    text: str = ""


@dataclass(frozen=True)
class GraphSummary:
    """One graph in the store, without reading the whole thing.

    `readable` is `False` for a file that is there and does not parse, and
    `detail` says why. A store with one broken file in it still lists.
    """

    name: str = ""
    readable: bool = True
    detail: str = ""
    state_count: int = 0
    #: The entry state's name.
    entry: str = ""


@dataclass(frozen=True)
class GraphValidation:
    """What compiling one graph against the attached board found.

    `pool_usage` against `pool_capacity` is the answer to "will this fit",
    which is a question about *this* board and cannot be answered without one.
    Warnings are things that compile and are probably not what was meant.
    """

    valid: bool = False
    detail: str = ""
    pool_usage: int = 0
    pool_capacity: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StateMachineConfigSummary:
    """One state machine config in the store — a session's document.

    The config names the graphs a session uses and carries the wiring for
    them. `board` is what it was written for, and a config loaded onto a
    different board is refused rather than adapted.
    """

    name: str = ""
    readable: bool = True
    detail: str = ""
    description: str = ""
    board: str = ""
    graph_names: list[str] = field(default_factory=list)
    input_line_count: int = 0
    output_line_count: int = 0


@dataclass(frozen=True)
class StateMachineConfigSummaries:
    """The config store, and which of them is loaded.

    `loaded` is a name and may be empty: a daemon with no config loaded is a
    normal daemon that has not been told what session this is yet.
    """

    configs: list[StateMachineConfigSummary] = field(default_factory=list)
    loaded: str = ""


# -- the session ----------------------------------------------------------------


@dataclass(frozen=True)
class LoadedStateMachineConfig:
    """The config this session is running, as loaded rather than as stored.

    `still_in_the_store` is `False` when the file was deleted or edited out
    from under a running session. The session keeps running on what it loaded —
    that is the point — and this is how you find out the two have diverged.
    """

    name: str = ""
    description: str = ""
    board: str = ""
    graph_names: list[str] = field(default_factory=list)
    still_in_the_store: bool = True


@dataclass(frozen=True)
class SessionState:
    """Everything about the session, in one read.

    `session_open` is the field to branch on. `open_seconds` is computed by the
    daemon deliberately: a caller subtracting `opened_at_unix_seconds` from its
    own clock gets a negative number on a box whose time has not settled.
    """

    state_machine_config: LoadedStateMachineConfig | None = None
    committed_set: CommittedGraphSet | None = None
    session_open: bool = False
    opened_at_unix_seconds: float | None = None
    open_seconds: float | None = None
    active_graph: str = ""
    stored_config_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OpenSessionResult:
    """What opening a session, or uploading a set, committed to the board.

    `slots` maps a graph's name to the slot it landed in, which is the number
    the board speaks. `elapsed_milliseconds` is how long the push took — worth
    having, because a large set on a slow link is the one operation in this
    API that takes visible time.
    """

    state_machine_config: str = ""
    set_version: int = 0
    slots: dict[str, int] = field(default_factory=dict)
    pool_usage: int = 0
    pool_capacity: int = 0
    elapsed_milliseconds: int = 0


@dataclass(frozen=True)
class LoadedConfigResult:
    """What loading a state machine config did.

    `wiring_pushed` is `False` with no board attached, and the config is still
    loaded: the daemon holds it and pushes the wiring when a board arrives.
    """

    loaded: str = ""
    wiring_pushed: bool = False
    line_map: LineMapView | None = None
    graph_names: list[str] = field(default_factory=list)


# -- trials ---------------------------------------------------------------------


@dataclass(frozen=True)
class DistributionPatch:
    """One named duration distribution, overridden for one trial.

    The graph declares the distribution; this changes its numbers without
    recompiling or re-pushing anything, which is what makes a staircase
    possible. Only the fields you set are changed — the rest are `None` and the
    graph's own values stand.
    """

    name: str
    minimum_ms: int | None = None
    maximum_ms: int | None = None
    mean_ms: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class ConfigureTrialResult:
    """A trial armed on the board, and what it was armed against.

    `set_version` and `graph_index` are the board's own identifiers for what
    will run. They are here so a record of the trial can say which compiled
    graph produced it, which a name alone cannot.
    """

    trial_id: int = 0
    graph: str = ""
    set_version: int = 0
    graph_index: int = 0
    elapsed_milliseconds: int = 0


@dataclass(frozen=True)
class StartTrialResult:
    """A trial started, with the board's own clock reading at the moment.

    `started_device_microseconds` is the board's clock, not the host's, and it
    is the one to align other recordings to: it is the same clock every
    `StateVisit` is stamped with.
    """

    trial_id: int = 0
    started_device_microseconds: int = 0


@dataclass(frozen=True)
class CancelTrialResult:
    """A cancel, and whether it landed.

    `cancelled` is `False` where the trial had already ended — which is not an
    error and is why this is a result rather than a refusal. `outcome_code` is
    the outcome the trial got, whichever way it ended.
    """

    trial_id: int = 0
    cancelled: bool = False
    outcome_code: int = 0


@dataclass(frozen=True)
class StateVisit:
    """One state the trial passed through, as the board timed it.

    Two durations, and they are not the same thing: `drawn_duration_ms` is what
    the state's distribution drew *before* the state ran, and
    `measured_duration_microseconds` is how long it actually took. The gap
    between them is the board's jitter and is the number worth watching.

    `fired_transition_position` is `None` where the state ended without a
    transition firing — a timeout, or the end of the trial.
    """

    state_name: str = ""
    #: Why the state ended, in the board's words.
    exit_cause: str = ""
    fired_transition_position: int | None = None
    fired_transition_target_state_name: str | None = None
    drawn_duration_ms: int = 0
    entered_device_microseconds: int = 0
    measured_duration_microseconds: int = 0


@dataclass(frozen=True)
class TrialResult:
    """A finished trial: how it ended, and the path it took to get there.

    **`visits` may be truncated, and `path_was_truncated` says so.** The board
    holds a bounded path buffer, and a graph that loops for long enough fills
    it. `total_visit_count` is how many there were; `visits` is how many came
    back. A trial whose path was cut is still a valid trial — the outcome is
    the outcome — and this is how you find out not to trust the path.
    """

    trial_id: int = 0
    outcome: TrialOutcome = TrialOutcome.NOT_STARTED
    cancel_reason: TrialCancelReason = TrialCancelReason.NONE
    total_duration_microseconds: int = 0
    visits: list[StateVisit] = field(default_factory=list)
    path_was_truncated: bool = False
    first_visit_sequence_number: int = 0
    total_visit_count: int = 0


# -- state and the trace --------------------------------------------------------


@dataclass(frozen=True)
class RigState:
    """The whole rig in one read: the link, the trial, the live lines.

    `newest_trace_entry_number` is here for one reason worth knowing: it is the
    number to pass to `watch_trace(since_entry_number=...)` so a subscription
    starts exactly where a read left off, with no gap and nothing repeated.

    `input_word` and `output_word` are the line states as bit words, straight
    off the board. They are `None` with no board attached.
    """

    connected: bool = False
    link_state: int = 0
    running: bool = False
    trial_id: int | None = None
    graph: str = ""
    state_name: str | None = None
    state_index: int | None = None
    input_word: int | None = None
    output_word: int | None = None
    scan: ScanHealth | None = None
    newest_trace_entry_number: int = 0


@dataclass(frozen=True)
class TraceEntry:
    """One entry in the trace ring.

    `kind` is the word to branch on — `state_visit` and `trial_result` are the
    two every consumer cares about, and :data:`KIND_STATE_VISIT` and
    :data:`KIND_TRIAL_RESULT` spell them.

    `payload` is whatever that kind carries, as a plain dict. It is
    deliberately not a union of dataclasses: the ring holds entries this client
    version has never heard of — a newer daemon, a kind added next month — and
    a client that refused to hand one back would make the trace unreadable for
    the sake of a type. The four named fields are the ones every kind has.
    """

    entry_number: int = 0
    kind: str = ""
    recorded_host_time: dt.datetime | None = None
    trial_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TraceWindow:
    """A read out of the trace ring, and where the ring is.

    `lost_entries_before` is `None` when nothing was lost and an entry number
    when the ring wrapped past what was asked for. Read it: a consumer that
    believed it saw everything is worse than one that knows it did not, and
    `read_trial_trace` can still answer for a whole trial.
    """

    entries: list[TraceEntry] = field(default_factory=list)
    newest_entry_number: int = 0
    oldest_entry_number_still_held: int = 0
    ring_capacity: int = 0
    lost_entries_before: int | None = None


@dataclass(frozen=True)
class Observer:
    """One thing currently watching a stream on this daemon.

    `fell_behind` is the interesting one: a subscriber the daemon could not
    keep feeding. It is visible here rather than only in the daemon's log
    because the thing that fell behind is usually not the thing reading this.
    """

    observer_id: str = ""
    name: str = ""
    #: Which stream — `trace`, `state`, `monitor`.
    stream: str = ""
    address: str = ""
    connected_at_unix_seconds: float = 0.0
    connected_seconds: float = 0.0
    delivered: int = 0
    fell_behind: bool = False


@dataclass(frozen=True)
class Observers:
    """Everything watching this daemon right now."""

    observers: list[Observer] = field(default_factory=list)
    count: int = 0


# -- recordings -----------------------------------------------------------------


@dataclass(frozen=True)
class RecordingSegment:
    """One continuous stretch of a recording, between a start and a pause.

    A recording is a list of these rather than one range because pausing is
    supported and a paused-then-resumed recording has a hole in it. The hole is
    a fact about the session and is kept rather than smoothed over.
    """

    from_entry_number: int | None = None
    to_entry_number: int | None = None
    started_host_time: dt.datetime | None = None
    ended_host_time: dt.datetime | None = None
    entry_count: int = 0


@dataclass(frozen=True)
class RecordingManifest:
    """A named recording taken off the trace ring.

    `state` is `recording`, `paused` or `stopped`. `kind_counts` is how many of
    each trace kind it holds, which is enough to know whether a recording is
    the one you want without reading it.

    `unreadable` is non-empty for a recording on disk that will not load, and
    then nothing else here means anything. A store with one broken recording in
    it still lists.
    """

    name: str = ""
    description: str = ""
    #: The config that was loaded when the recording was taken.
    state_machine_config: str = ""
    created_unix_seconds: float = 0.0
    created_host_time: dt.datetime | None = None
    state: str = ""
    segments: list[RecordingSegment] = field(default_factory=list)
    entry_count: int = 0
    kind_counts: dict[str, int] = field(default_factory=dict)
    unreadable: str = ""


@dataclass(frozen=True)
class Recordings:
    """The recording store, and the one being written to.

    `active` is `None` when nothing is being recorded, which is the common
    case: a rig records deliberately, not continuously.
    """

    active: RecordingManifest | None = None
    recordings: list[RecordingManifest] = field(default_factory=list)


@dataclass(frozen=True)
class RecordingEntries:
    """A page of one recording's entries.

    `offset` is the offset that was served, which is not always the one asked
    for. `segments` is repeated here so a page can be placed without reading
    the manifest again.
    """

    name: str = ""
    offset: int = 0
    entries: list[TraceEntry] = field(default_factory=list)
    entry_count: int = 0
    segments: list[RecordingSegment] = field(default_factory=list)


# -- the rig configuration ------------------------------------------------------


@dataclass(frozen=True)
class RigConfiguration:
    """The **rig config**: this box's hardware, not this session's experiment.

    The two are never the same file and this client never confuses them
    (`contracts/DAEMON_LAYOUT.md` §1). This one is TOML on disk, changes when
    the hardware does, and is not written back wholesale from the API — which
    is why there is a patch type and no `write_configuration`.

    The session's document is a **state machine config**: JSON, in the store,
    addressed by name, and loaded with `load_config`.
    """

    device_target: str = ""
    device_baud: int = 0
    device_timeout_seconds: float = 0.0
    expected_board: str = ""
    connect_on_startup: bool = False
    startup_state_machine_config: str = ""
    graph_mode: str = ""
    trace_ring_entries: int = 0
    heartbeat_seconds: float = 0.0
    trace_directory: str = ""
    graph_store_directory: str = ""
    recording_directory: str = ""
    state_machine_config_directory: str = ""


@dataclass(frozen=True)
class RigConfigurationPatch:
    """The five fields of the rig config the API may change.

    Five rather than all of them, on purpose: the directories are where a
    running daemon's files *are*, and changing one over the network would move
    a store out from under an open session. Those are edited on the box.

    Only the fields you set are changed. `None` means "leave it".
    """

    device_target: str | None = None
    device_baud: int | None = None
    expected_board: str | None = None
    graph_mode: str | None = None
    startup_state_machine_config: str | None = None


@dataclass(frozen=True)
class RigConfigurationUpdate:
    """The configuration after a patch, and what the patch cost.

    `reconnected` means the device link was torn down and brought back —
    changing `device_target` does that. `until_restart` means the change is in
    effect now but was not written to the file, so a restart loses it.
    """

    configuration: RigConfiguration | None = None
    reconnected: bool = False
    until_restart: bool = False


# -- health ---------------------------------------------------------------------


@dataclass(frozen=True)
class Health:
    """Is this daemon up, and does it have a board.

    Two booleans rather than one, because they fail separately and the answer
    to "why is nothing happening" is usually the second: a daemon whose board
    is unplugged is still the thing you ask.
    """

    ok: bool = False
    device_connected: bool = False


#: The two trace kinds every consumer branches on. Spelled here so a typo is a
#: `NameError` rather than a subscription that quietly never matches.
KIND_STATE_VISIT = "state_visit"
KIND_TRIAL_RESULT = "trial_result"

#: What a subscription calls itself when nothing else was said. It shows up in
#: `read_observers()`, which is the point: an unnamed observer on a rig with
#: three scripts running is a question nobody can answer.
DEFAULT_OBSERVER_NAME = "statemachined-client"
