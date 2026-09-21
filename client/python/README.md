# statemachined-client — the Python client for statemachined

Talks to a [statemachined](https://github.com/braemons/statemachined) daemon
over gRPC and hands back types.

```python
from statemachined_client import StatemachinedClient

with StatemachinedClient("rig-3.local") as rig:
    rig.open_session()
    rig.upload_graph_set(["go-nogo", "2afc"])

    with rig.watch_trace() as trace:
        rig.configure_trial(1, graph="go-nogo", cap_milliseconds=30_000)
        rig.start_trial(1)
        for entry in trace:
            if entry.kind == "trial_result" and entry.trial_id == 1:
                break

    for entry in rig.read_trial_trace(1):
        print(entry.kind, entry.payload.get("state_name"))
```

**No protobuf type crosses this package's edge.** The generated code is private,
in `statemachined_client._proto`; `statemachined_client.api_types` is the public
vocabulary. You should not have to learn a generated API to read a state visit.

The interface those types come from is `proto/statemachined/v1/` in the daemon's
repository, authored by hand — types *and* rpcs. `docs/reference/api.md` there
says what each rpc is for.

## The other way to drive a board

This is one of two. The other is `statemachined.device`, in the daemon's own
distribution, which opens the serial port itself. **They are different classes
on purpose and not two backends behind one interface**: a daemon outlives the
script that spoke to it, so it can hold a bounded trace ring, take named
recordings off it, keep a graph store on disk and say who else is watching. A
direct connection has none of that — your process was the only listener, and
what it did not keep is gone. A single facade would have to answer
`start_recording()` on both, and on one of them the answer would be a fiction.

What the two *do* share is the trial loop, the graph set, the wiring, autorun
and save, because those belong to the board rather than to the daemon.

## Three things worth knowing

**Subscribe before you arm.** `watch_trace()` with no `since_entry_number`
begins at the newest entry, and a trial that ends quickly ends before you are
watching. `RigState.newest_trace_entry_number` is the number to resume from.

**The deadline is yours.** Nothing on the rig waits for a subscriber or holds a
trial for one. Only the side that knows a trial is in flight can tell "not yet"
from "never".

**A gap in the ring is recoverable.** `read_trial_trace(id)` answers exactly
whatever the stream did, so nothing is lost for good — but a consumer that
believed it saw everything is worse than one that knows it did not, which is
why `TraceWindow.lost_entries_before` is a field and not a silence.

## Install

```console
$ pip install git+https://github.com/braemons/statemachined.git#subdirectory=client/python
```

Two runtime dependencies, `grpcio` and `protobuf`. Not the daemon: talking to a
rig should not mean installing one, and it should not mean installing pydantic
either.

## `statemachinectl`

The same client as a command line. `--rig` takes `host` or `host:port`.
Everything prints JSON, so it pipes into `jq`.

```console
$ statemachinectl --rig rig-3.local state
$ statemachinectl --rig rig-3.local device
$ statemachinectl --rig rig-3.local graphs
$ statemachinectl --rig rig-3.local trace --follow
```

## Licence

LGPL-3.0-or-later — the *library's* licence, not the daemon's. The daemon is
AGPL because it is a network service; this is a client you import into your own
script, and importing it places nothing of yours under copyleft.
