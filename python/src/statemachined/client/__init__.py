# SPDX-License-Identifier: LGPL-3.0-or-later
"""Talking to a statemachined **daemon**, over HTTP and WebSockets.

One of the two ways to drive a rig from Python, and the one to reach for when
something other than your script owns the board. The other is
`statemachined.device`, which opens the port itself.

```python
from statemachined.client import StatemachinedClient

with StatemachinedClient("http://rig-3.local:8081") as rig:
    rig.session.upload_graph_set(["go-nogo", "2afc"])

    with rig.trace.subscribe("my-experiment") as stream:
        rig.trial.configure(1, graph="go-nogo", cap_milliseconds=30_000)
        rig.trial.start(1)
        stream.wait_for_trial(1, timeout_seconds=35)

    for entry in rig.trace.for_trial(1):
        print(entry["kind"], entry.get("state_name"), entry.get("outcome"))
```

**Why this is a different class from `StatemachinedDevice` and not a second
backend behind one interface.** A daemon can do things a direct connection
cannot, and they are not incidental: it keeps a bounded trace ring, it takes
named recordings off that ring, it holds a graph store and saved configs on
disk, and it can tell you who else is watching. All of that exists because the
daemon **outlives the script that spoke to it**. A direct connection has no ring
to record from -- your process was the only listener, and what it did not keep
is gone. A single facade would have to answer `rig.recordings.start()` on both,
and on one of them the answer would be a fiction.

What the two *do* share is the trial loop, the graph set, the wiring, autorun
and save -- because those are the board's, not the daemon's. A paradigm moves
between them; a record-keeping strategy does not.

Three things are worth knowing before writing anything against this one.

**Subscribe before you arm.** A subscription opened after a trial started begins
at the newest entry, and a trial that ends quickly ends before you are watching.

**The deadline is yours.** Nothing on the rig waits for a subscriber, holds a
trial for one, or retries -- opening the socket is the whole of subscribing.
Only the side that knows a trial is in flight can tell "not yet" from "never".

**A dropped notification is recoverable.** `trace.for_trial(id)` answers exactly
whatever the stream did, which is why a lost subscription is an exception you
can catch rather than a session you have lost.

See `docs/reference/api.md` for the API this wraps, and `http_client.py` for why it is
shaped the way that document is.
"""

from ._transport import DEFAULT_TIMEOUT_SECONDS, UPLOAD_TIMEOUT_SECONDS
from .http_client import (
    DEFAULT_BASE_URL,
    DeviceApi,
    GraphApi,
    RecordingApi,
    RigConfigApi,
    SessionApi,
    StateMachineConfigApi,
    StatemachinedClient,
    TraceApi,
    TrialApi,
)
from .errors import (
    Conflict,
    Invalid,
    NotConnected,
    NotFound,
    Refused,
    StatemachinedError,
    TraceStreamLost,
    TransportError,
)
from .trace import (
    DEFAULT_OBSERVER_NAME,
    KIND_STATE_VISIT,
    KIND_TRIAL_RESULT,
    TraceSubscription,
    entries,
    finished_trials,
    is_a_finished_trial,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_OBSERVER_NAME",
    "DEFAULT_TIMEOUT_SECONDS",
    "KIND_STATE_VISIT",
    "KIND_TRIAL_RESULT",
    "UPLOAD_TIMEOUT_SECONDS",
    "Conflict",
    "DeviceApi",
    "GraphApi",
    "Invalid",
    "NotConnected",
    "NotFound",
    "RecordingApi",
    "Refused",
    "RigConfigApi",
    "SessionApi",
    "StateMachineConfigApi",
    "StatemachinedClient",
    "StatemachinedError",
    "TraceApi",
    "TraceStreamLost",
    "TraceSubscription",
    "TransportError",
    "TrialApi",
    "entries",
    "finished_trials",
    "is_a_finished_trial",
]
