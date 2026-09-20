# SPDX-License-Identifier: LGPL-3.0-or-later
"""Talking to a statemachined **daemon**, over gRPC.

One of the two ways to drive a rig from Python, and the one to reach for when
something other than your script owns the board. The other is
`statemachined.device`, in the daemon's own distribution, which opens the
serial port itself.

```python
from statemachined_client import StatemachinedClient

with StatemachinedClient("rig-3.local") as rig:
    rig.load_config("go-nogo-session")
    rig.open_session()

    with rig.watch_trace(rig.read_state().newest_trace_entry_number + 1) as trace:
        rig.configure_trial(1, graph="go-nogo", cap_milliseconds=30_000)
        rig.start_trial(1)
        for entry in trace:
            if entry.kind == KIND_TRIAL_RESULT and entry.trial_id == 1:
                break

    result = rig.read_trial_result(1)
    print(result.outcome.name, len(result.visits))
```

**Why this is a different class from `StatemachinedDevice` and not a second
backend behind one interface.** A daemon can do things a direct connection
cannot, and they are not incidental: it keeps a bounded trace ring, it takes
named recordings off that ring, it holds a graph store and saved configs on
disk, and it can tell you who else is watching. All of that exists because the
daemon **outlives the script that spoke to it**. A direct connection has no
ring to record from — your process was the only listener, and what it did not
keep is gone. A single facade would have to answer `start_recording()` on both,
and on one of them the answer would be a fiction.

What the two *do* share is the trial loop, the graph set, the wiring, autorun
and save — because those are the board's, not the daemon's. A paradigm moves
between them; a record-keeping strategy does not.

Three things are worth knowing before writing anything against this one.

**Subscribe before you arm.** `watch_trace` carries the backlog, so a
subscription opened late is not a subscription that lost the trial — but only
for as long as the ring holds it. A trial you care about is one you were
watching for.

**The deadline is yours.** Nothing on the rig waits for a subscriber, holds a
trial for one, or retries — opening the stream is the whole of subscribing.
Only the side that knows a trial is in flight can tell "not yet" from "never".

**A gap is recoverable, and it is not announced.** Compare `entry_number`
against the one you expected; `read_trial_trace(id)` answers exactly whatever
the stream did, which is why a ring that wrapped is an exception you can
recover from rather than a session you have lost.

See `docs/reference/api.md` in the daemon's repository for the interface this
wraps, and `proto/statemachined/v1/` for the description both sides are
generated from.
"""

from .api_types import (
    DEFAULT_OBSERVER_NAME,
    KIND_STATE_VISIT,
    KIND_TRIAL_RESULT,
    Autorun,
    CancelTrialResult,
    CloseSessionResult,
    CommittedGraphSet,
    ConfigureTrialResult,
    DeviceCapacities,
    DeviceState,
    DistributionPatch,
    FirmwareVersions,
    GraphPoolCounts,
    GraphSummary,
    GraphValidation,
    GraphWarning,
    Health,
    InputLine,
    LineMapView,
    LinkHealth,
    LoadedConfigResult,
    LoadedStateMachineConfig,
    Observer,
    Observers,
    OpenSessionResult,
    OutputLine,
    RecordingEntries,
    RecordingManifest,
    Recordings,
    RecordingSegment,
    RigConfiguration,
    RigConfigurationPatch,
    RigConfigurationUpdate,
    RigState,
    SaveSettingsResult,
    ScanHealth,
    SerialMonitorEntry,
    SerialMonitorWindow,
    SessionState,
    StartTrialResult,
    StateMachineConfigSummaries,
    StateMachineConfigSummary,
    StateVisit,
    StoredFile,
    TraceEntry,
    TraceWindow,
    TrialCancelReason,
    TrialOutcome,
    TrialResult,
    WriteLineMapResult,
)
from .daemon_client import (
    DEFAULT_PORT,
    DEFAULT_WEB_PORT,
    DaemonStreamSubscription,
    StatemachinedClient,
)
from .daemon_refusals import (
    DaemonIsUnavailable,
    DaemonRefusedTheRequest,
    NoBoardIsAttached,
    NoSuchDocument,
    TheRigIsNotInAStateForThat,
)

__all__ = [
    "DEFAULT_OBSERVER_NAME",
    "DEFAULT_PORT",
    "DEFAULT_WEB_PORT",
    "KIND_STATE_VISIT",
    "KIND_TRIAL_RESULT",
    "Autorun",
    "CancelTrialResult",
    "CloseSessionResult",
    "CommittedGraphSet",
    "ConfigureTrialResult",
    "DaemonIsUnavailable",
    "DaemonRefusedTheRequest",
    "DaemonStreamSubscription",
    "DeviceCapacities",
    "DeviceState",
    "DistributionPatch",
    "FirmwareVersions",
    "GraphPoolCounts",
    "GraphSummary",
    "GraphValidation",
    "GraphWarning",
    "Health",
    "InputLine",
    "LineMapView",
    "LinkHealth",
    "LoadedConfigResult",
    "LoadedStateMachineConfig",
    "NoBoardIsAttached",
    "NoSuchDocument",
    "Observer",
    "Observers",
    "OpenSessionResult",
    "OutputLine",
    "RecordingEntries",
    "RecordingManifest",
    "RecordingSegment",
    "Recordings",
    "RigConfiguration",
    "RigConfigurationPatch",
    "RigConfigurationUpdate",
    "RigState",
    "SaveSettingsResult",
    "ScanHealth",
    "SerialMonitorEntry",
    "SerialMonitorWindow",
    "SessionState",
    "StartTrialResult",
    "StateMachineConfigSummaries",
    "StateMachineConfigSummary",
    "StateVisit",
    "StatemachinedClient",
    "StoredFile",
    "TheRigIsNotInAStateForThat",
    "TraceEntry",
    "TraceWindow",
    "TrialCancelReason",
    "TrialOutcome",
    "TrialResult",
    "WriteLineMapResult",
]
