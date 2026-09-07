# statemachined — the HTTP API

> **Status:** specification. Milestone M4f in [`DAEMON.md`](DAEMON.md); the
> daemon is being written against this document, not the other way round —
> which is the same order [`PROTOCOL.md`](PROTOCOL.md) was written in, and for
> the same reason: an interface argued for after the fact is an interface whose
> shape is an accident of the first implementation.

Two callers and they want different things.

- **triald** drives the trial loop. It knows the session, the trial types, the
  ITI and which paradigm a trial is; it does not know that a device exists. What
  it needs from here is small, stable, and in the critical path of every trial.
- **A person** — through the web UI (§[DAEMON.md 5](DAEMON.md)) or `curl` — needs
  everything else: what board is attached, which pin is the left lever, what the
  machine is doing right now, what the last trial did.

The second is why this is an HTTP API and not a library. A translator can be a
library; a thing you can ask *"is the valve wired to A0 and did it open"* at two
in the morning cannot.

Everything here is JSON. The models are the ones in
`daemon/src/statemachined/model/`, so the generated schema at `/openapi.json` is
not a second description of them that can drift. **The web UI uses only this
API** — no private route — which is what keeps the UI an honest test of it.

---

## 1. What this API is not

**Not a second decision authority.** `PLAN.md` is explicit: `precise_fixation`
and `frame_loss` can each veto acceptance on their own, and triald collects
them. Nothing here sets a veto field, and nothing here decides whether a trial
was good. The daemon reports what the device measured.

**Not the trial-type authority.** triald chooses the trial type and therefore
chooses the graph. Every call below names a graph by **name**; no caller ever
sees or supplies a slot index. See DAEMON.md §3.1.

**Not a timing authority.** Every number about a trial came off the device's
clock. Where this API reports a host time it is an *estimate*, it is labelled
one, and it carries its uncertainty (§6).

---

## 2. Conventions

| | |
|---|---|
| Errors | RFC-7807-shaped: `{"error": "<code>", "detail": "<sentence>", "context": "<what to change>"}`, with a matching HTTP status. Every refusal names what to change — the same rule the wire's errors follow |
| `409 Conflict` | the device refused, or the daemon's state does not admit the call (no set uploaded, no trial armed). The body's `error` is the device's own code where there was one |
| `503 Service Unavailable` | no device is connected |
| `422 Unprocessable Content` | the request did not validate. FastAPI's own shape, since it names the field |
| Times | device microseconds are integers named `*_device_microseconds`; host times are ISO-8601 UTC strings named `*_host_time`, always beside the raw value they were estimated from |
| Durations | milliseconds where a person set them, microseconds where the device measured them, and the name says which |

---

## 3. The device

### `GET /api/device`

What is attached, what it can hold, and how the link is behaving.

```jsonc
{
  "connected": true,
  "target": "socket://rig-3.local:5000",
  "board": "uno_r4_minima",
  "firmware_version": "0.1.0",
  "protocol_version": 1,
  "measured_scan_hz": 9871,
  "capabilities": { "max_states": 32, "max_graphs": 20, "max_path": 255, "...": 0 },
  "has_wiring": true,
  "pin_labels_came_from": "device",
  "committed_set": { "set_version": 7, "graph_names": ["go-nogo", "2afc"] },
  "link": { "connection_count": 1, "dropped_lines": 0, "bad_lines": 0 },
  "scan": { "hz": 9871, "overruns": 4, "worst_gap": 2, "tx_stalls": 0 },
  "uptime_device_microseconds": 90210000
}
```

`capabilities` is read from the board's `hello_ack` and never assumed: the
reference board ships two images and a Teensy is a different set of numbers
again.

`scan` is diagnosis rather than control, and it is the honest half of the
timing claim: a board that quietly misses scans looks exactly like a board that
is fine, so a missed scan is counted and reported.

`pin_labels_came_from` is `device` when the board answered `pins`
(PROTOCOL.md §3.6) and this daemon therefore knows which pin each line is, and
`assumed` when it fell back to its own table for firmware older than that
command. `unknown` is a board neither knows. A client showing a pin label should
show that difference: one is the board's word and the other is a belief.

### `GET /api/device/lines`

Every line this rig has, by name, with what the wiring does to it and what it is
doing now.

```jsonc
{
  "input_lines": [
    { "name": "lever_left", "line_index": 4, "pin_label": "D6",
      "reads_active_low": false, "is_enabled": true, "debounce_milliseconds": 5,
      "is_high_now": false }
  ],
  "output_lines": [
    { "name": "reward_valve", "line_index": 3, "pin_label": "A0",
      "safe_level_is_high": true, "is_high_now": true }
  ],
  "board_input_pins": ["D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9"],
  "board_output_pins": ["D10", "D11", "D12", "A0", "A1", "A2", "A3", "A4"],
  "pin_labels_came_from": "device"
}
```

`is_high_now` comes from `state_report`'s `io`, which is the **only** way
anything outside the device can check that a graph's line numbers reach the pins
somebody wired: there is no read-back path from a pin, and the output word is
the engine's own shadow rather than a measurement.

`board_input_pins` and `board_output_pins` are **the board's own answer**,
indexed by line number: `board_input_pins[4]` is what the firmware says input
line 4 is. They sit beside the two lists rather than inside them, because a
client reads this object, edits it, and PATCHes it back — and `LineMap` refuses
members it does not declare. `is_high_now` is the one member added to a line
here, and that is a contract.

The line's own `pin_label` is what the *config* calls the pin. The two agree or
the daemon refused to connect (PROTOCOL.md §3.6). What a client should do with
the difference is offer these lists as the **choices** — the assignment of a
name to a pin is the rig's to make, the label itself is the board's — and,
where `pin_labels_came_from` is not `device`, show that its labels are an
assumption. The web UI's Lines panel is the worked example.

### `PATCH /api/device/lines`

Change the line map. The body is a whole `LineMap` (§`model/line_map.py`).

**Renaming a line is free.** Names are the daemon's alone and never reach the
wire, so a rename changes no graph and needs no upload — which is what lets a
paradigm be authored against words instead of a pinout.

**A line may name a pin instead of a number.** `{"name": "lever", "pin_label":
"D6"}` with no `line_index` is resolved against what the board answered, which
is the form worth using: a bit position is not written anywhere on the hardware
and `D6` is. Where both are given they are checked, and a body whose pin and
line contradict the board — or which names a pin this board does not have — is
refused `422 line_map_does_not_match_the_board`, **before anything is kept**, so
the rig carries on with the map it had.

**The rest is the wiring**, and it is pushed to the device in the same call:
invert, enable, debounce and the output safe levels (PROTOCOL.md §3.5). It is
refused with `409` while a trial is armed or running, because the conditioning
it changes is read by the scan.

> **It does not survive a power cycle yet.** Data flash is DAEMON.md's M7. Until
> then a reset returns the board to its compile-time safe levels, and the daemon
> pushes the wiring again on every connect.

### `GET /api/device/monitor`  ·  `WS /api/device/monitor/stream`

Every line in and out of the serial port, as it went.

```jsonc
{
  "lines": [
    { "entry_number": 0, "direction": "to_device",
      "line": "{\"msg_type\":\"hello\",\"message_id\":1,...,\"crc\":\"9AD5\"}",
      "recorded_host_time": "2026-09-04T10:55:02.180508Z" },
    { "entry_number": 1, "direction": "from_device", "line": "{\"msg_type\":\"hello_ack\",...}",
      "recorded_host_time": "2026-09-04T10:55:02.186133Z" }
  ],
  "newest_entry_number": 33,
  "oldest_entry_number_still_held": 0,
  "ring_capacity": 4000,
  "lost_lines_before": null
}
```

For the moment the layers stop agreeing: `/api/device/lines` says the valve is
line 3, the valve is not opening, and the question is what actually crossed the
wire. Nothing here interprets anything — these are whole lines, CRC included,
in the order they went, taken at the transport, so a line nothing could parse is
in here too.

**Always recording**, because the alternative is not: a fault that happens once
an hour is not reproducible on demand, and a monitor somebody has to switch on
first is off when the interesting thing happens. It costs one string per line
against a bounded deque.

**A log, not the record.** It is the last few thousand lines and then the oldest
go, and none of it is written to disk. What a trial *did* is §7's trace, which
is kept.

The stream is **not coalesced**, exactly as the trace's is not, and a client
that falls out of the ring is told the range it lost and the socket closes.
`since_entry_number` and `limit` page the GET; `lost_lines_before` is non-null
when the cursor asked for is older than anything still held.

Nothing sends. A serial terminal that could type at the board would be a second
host on a link whose whole design is one command in flight (PROTOCOL.md §1.2),
and every command worth sending has a route above — each of which shows up here
when it goes.

### `GET /api/device/firmware`

The version running against what the installed package ships, and whether they
agree. See DAEMON.md §6.3. Flashing is not here and is deferred: it means
dropping the port mid-session, which is a different risk from anything else the
daemon does.

---

## 4. Graphs

### The store

| | |
|---|---|
| `GET /api/graphs` | every graph the store holds, by name, with its state count |
| `GET /api/graphs/{name}` | the authored form, exactly as `model/graph_definition.py` defines it |
| `PUT /api/graphs/{name}` | write one. Validated on the way in, so the store never holds a graph that could not be run |
| `DELETE /api/graphs/{name}` | remove one. Refused with `409` if it is in the committed set |

The store is `/var/lib/braemons/statemachined/graphs/`, one JSON file per graph, and that
is deliberate: it is greppable, it is diffable, and a rig at 2 a.m. with no
network can be fixed with an editor.

### `POST /api/graphs/{name}/validate`

Every rule, plus **this device's** `caps`. Changes nothing and uploads nothing.

```jsonc
{ "valid": true, "pool_usage": { "states": 4, "transitions": 2, "...": 0 },
  "pool_capacity": { "states": 32, "...": 0 },
  "warnings": [ { "kind": "any_clause_has_no_effect", "state": "Respond",
                  "transition": 0, "lines": ["lever_left"],
                  "detail": "'lever_left' is in both `all` and `any`, so ..." } ] }
```

`warnings` is for a graph that is legal, uploads, runs — and is narrower than
its author thinks. The only one so far is a line in both `all` and `any`: the
masks are ANDed, so `all` already requires that line high, the `any` clause is
satisfied whenever the predicate could fire at all, and every *other* line in
`any` is ignored. `all: [L], any: [L, M, N]` means `L`. It is reported rather
than refused because the graph does exactly what the masks say, and an editor
that refuses a redundancy mid-edit is one people work around.

Two predicates *are* refused, by `model/graph_definition.py`, wherever a graph
arrives: a line in both `all` and `none`, and an `any` clause every one of whose
lines is in `none`. Both can never fire, and nothing downstream can see it — the
graph uploads, the state runs, and the only way out never happens.

The capacity half is the useful half: *"you have room for two more graphs"* is
what a person setting up a session wants, and it is why usage is reported rather
than only checked.

### `POST /api/graphs/{name}/upload`

Compile and upload one graph **as a set of one**, and commit it. → `set_version`.

The bench and UI path: trying a graph out. It replaces whatever set is
committed, so it is refused with `409` while a session's set is in place —
losing a session's paradigms because somebody previewed a graph is not a
recoverable mistake.

### `POST /api/session/graphs`

**Where a session is allowed to fail, and that is the point.**

```jsonc
// request
{ "graph_names": ["go-nogo", "2afc", "catch"] }

// response
{ "set_version": 8, "slots": {"go-nogo": 0, "2afc": 1, "catch": 2},
  "pool_usage": {"states": 15, "...": 0}, "pool_capacity": {"states": 32, "...": 0},
  "elapsed_milliseconds": 2140 }
```

triald declares the names a session will use — the union of what its loaded
sets' trial types reference, which only triald knows — and the daemon compiles
every one, checks the **summed** pool usage against the device's `caps`, uploads
the set and commits it. Before an animal is in the booth.

The alternative is discovering at trial 40 that one trial type names a graph
with forty states on a thirty-two-state board, and losing the session to it.

It is the slowest call in this API by a wide margin — tens of seconds on a UART
rig (DAEMON.md §3.2) — and the one the UI should show a progress bar for. It is
also the *only* place a graph is uploaded during a session: `configure` never
is.

> **A failed upload leaves the board holding nothing.** Two sets do not fit in
> 32 KB, so the device fills the live one (PROTOCOL.md §3.2). If this call
> fails, no trial can be armed until a set uploads successfully, and every
> output sits at its safe level meanwhile. That is worse than the old behaviour
> and much more visible, which is the trade.

---

## 5. The trial loop

triald drives; statemachined reports.

### `POST /api/trial/configure`

```jsonc
// request
{ "trial_id": 193, "graph": "go-nogo", "cap_milliseconds": 30000,
  "start_source": "serial",
  "distribution_patches": [{"name": "foreperiod", "minimum_ms": 250, "maximum_ms": 900}] }

// response
{ "trial_id": 193, "graph": "go-nogo", "set_version": 8, "graph_index": 0,
  "elapsed_milliseconds": 3 }
```

**`graph` is a name.** The daemon resolves it to a slot in the committed set —
it built the set, so it is the only process that knows. A name the store does
not hold is refused rather than guessed at; a name outside the committed set is
refused too, and the refusal says which name and which set, because a session
that declared its graphs and then asks for a fourth has a misconfigured trial
type.

**Nothing is uploaded.** `elapsed_milliseconds` is reported anyway, so an arm
that took longer than it should is a number per trial rather than an inference.

`cap_milliseconds` is a wall-clock cap on the whole trial. It stays regardless of
the graph: validation cannot tell a ten-second foreperiod from a hang.

`distribution_patches` change the parameters of named distributions for this
trial only, and are reverted when it ends. They cannot change the *shape* of
anything — that would be a different graph.

### `POST /api/trial/start`

`{"trial_id": 193}`. Refused unless the device is armed for that id and the
configured `start_source` admits serial. **No trial runs that the device was not
confirmed configured for.**

### `POST /api/trial/cancel`

`{"trial_id": 193}`.

A cancel that races a terminal state comes back with the **real outcome**, not a
fabricated `CANCELLED`. The daemon passes that through unchanged: asking to
cancel and being told `HIT` is triald's to cope with, and the alternative is a
record claiming a trial was cancelled when the animal had already responded.

### `GET /api/trial/result`

The last completed trial, read back into names.

```jsonc
{
  "trial_id": 193, "outcome": "HIT", "cancel_reason": "NONE",
  "total_duration_microseconds": 1483200,
  "path_was_truncated": false, "first_visit_sequence_number": 0, "total_visit_count": 2,
  "visits": [
    { "state_name": "Foreperiod", "exit_cause": "timeout",
      "fired_transition_position": null, "fired_transition_target_state_name": null,
      "drawn_duration_ms": 500, "entered_device_microseconds": 500120,
      "measured_duration_microseconds": 183044 }
  ]
}
```

Named against the graph that **actually ran** — the compiled set the daemon
uploaded — rather than against whatever the store holds today, which is what
keeps a renamed state from mislabelling last week's data.

### Outbound: `POST {triald}/api/trial/outcome`

One call, made by the daemon when a trial ends, carrying `outcome`,
`manipulandum`, `reaction_time_ms`, `terminating_interval`, `reward_ms` and
`simulated: false`. **`precise_fixation` and `frame_loss` are left at their
defaults**: the daemon has never heard of the eye monitor or vstimd, and
acquiring an opinion about them would make it a second decision authority.

> **This contradicts triald's own `dev/API.md`**, which says of the trial loop
> *"**Pull, not push.** The caller asks for a trial when it is ready."* Under the
> arrangement here triald calls `configure`/`start`, which makes triald the
> clock — the precise thing it declined to be. It is still the right split, but
> it needs an amendment there rather than a silent divergence. DAEMON.md §4.3
> flags it; open question 3 is whose document changes.

---

## 6. What it is doing right now

### `GET /api/state`

One snapshot: the link, the device's link state, the armed trial, the state the
machine is in **by name**, and the live input and output words.

### `WS /api/stream`

The same snapshot as frames, **coalesced**. A client that falls behind gets the
current state rather than a backlog of stale ones — triald's convention, and for
its reason: a slow browser tab must not hold up a session. Coalescing a state
snapshot loses nothing, because the latest one is the whole truth.

---

## 7. The trace

A timestamped log of every state the machine entered, kept whether or not
anybody asked. DAEMON.md §4.6 is the design; this is its surface.

| | |
|---|---|
| `GET /api/trace` | the ring, newest last. `?since_entry_number=` and `?limit=` |
| `GET /api/trace/trial/{trial_id}` | one trial's visits |
| `WS /api/trace/stream` | every entry as it arrives, **not** coalesced |

```jsonc
{ "entry_number": 4172, "kind": "visit", "device_sequence_number": 2,
  "trial_id": 193, "graph": "go-nogo", "set_version": 8,
  "state_name": "Foreperiod", "exit_cause": "timeout",
  "fired_transition_target_state_name": "Cue",
  "drawn_duration_ms": 500,
  "entered_device_microseconds": 500120,
  "unwrapped_device_microseconds": 4795500120,
  "entered_host_time": "2026-09-03T14:22:07.481932Z",
  "host_time_uncertainty_microseconds": 180 }
```

**`entry_number` is the daemon's, and it is what `since_` means.** The device's
`seq` counts visits within a *run* and restarts at zero every trial, so it
cannot address a position in a log that spans a session. Both are carried:
`device_sequence_number` is what a gap in the stream is detected with, and
`entry_number` is what a cursor is.

**`kind` is there because the device is not the only thing worth timestamping.**
The daemon puts its own events in the same ring: `configure`, `start`, `cancel`
and their host times, link loss and reconnection, an upload and what it cost.
Aligning an external signal to trial 193 needs to know when trial 193 was armed,
not only which states it visited.

**Three timebases, side by side, on purpose.** `entered_device_microseconds` is
what the device said and is the evidence. `unwrapped_device_microseconds` is
that made monotonic across the ~71-minute wrap. `entered_host_time` is an
*estimate* and is null until a `ping` has been answered — a made-up offset would
be indistinguishable in the record from a measured one.

**The stream is not coalesced, and the boundary is visible.** A client slow
enough to fall out of the ring has genuinely lost data and is told so, with the
`entry_number` range that is gone, rather than handed a shorter answer that
looks complete. Buffering per client is how a monitoring aid becomes the thing
that fills the Pi's memory.

**This is not the `.tdr` and must not grow into one.** triald writes the trial
record; this is finer grained, one line per state visit, and it **joins to the
`.tdr` on `trial_id`** — which is the entire reason `trial_id` is on the wire.

---

## 8. Configuration

Two configurations, in two places, and which is which is decided by one
question: **may the daemon write it?**

| | the rig config | a state-machine config |
|---|---|---|
| what it says | what the box is | what the box is doing today |
| where | `/etc/braemons/statemachined-rig-config.toml` | `/var/lib/braemons/statemachined/configs/*.config.json` |
| holds | device target, expected board, triald URL, directories, timeouts | the line map, and the graphs |
| edited by | a person with an editor | the web UI |
| written by the daemon | **never** | on save |
| API | `/api/config` | `/api/state-machine-configs` |

The split is the one vstimd makes between its rig-config and its scene-configs,
and the reason is mechanical: a conffile a daemon rewrites is a file that fights
the package manager on every upgrade, and the line map is exactly the thing
somebody adjusts at the bench on a Tuesday.

### `GET·PATCH /api/config`

The device target URL, the expected board, the triald base URL, the seed policy,
whether to connect on startup, which state-machine config to load at startup,
`graph_mode` (DAEMON.md §3.2) and `trace_ring`. **No line map**: that is a
state-machine config.

A `PATCH` that changes the device target or the expected board reconnects, and
is refused while a trial is armed. It changes the running daemon and **not the
file** — the reply says `until_restart` — because `/etc/braemons` is a conffile
this daemon does not write.

### `GET /api/state-machine-configs`

Every saved config, as summaries — name, description, board, graph names, line
counts — plus `loaded`, the name of the one this rig is running. A config that
will not parse is listed with the reason rather than omitted: a file you cannot
see is a file you cannot fix.

### `GET·PUT·DELETE /api/state-machine-configs/{name}`

Read, save and remove one. A `PUT` writes a file and **touches no hardware**;
the name in the path and the name in the body must agree. Deleting the loaded
config is allowed and does not stop the rig — deleting a file is not a request
to stop an experiment — and `GET /api/session` then reports
`is_still_in_the_store: false`.

### `POST /api/state-machine-configs/{name}/load`

Make one the rig's: resolve its line map against the board, and push the wiring.
Refused `422 state_machine_config_does_not_match_the_board` if this board does
not have those pins — **before anything is kept**, so the rig carries on with
the map it had. Refused `409 busy` while a trial is armed or running.

The graphs are *not* uploaded here. Loading says what this rig is; opening a
session is what puts it on the device, and keeping them apart is what lets
somebody load a config to look at it without disturbing a board.

---

## 9. The pages this daemon serves

Not part of the API, and listed here because they share its origin and its CORS
rules. dev/DAEMON.md §5 is the design.

| | |
|---|---|
| `GET /` | the rig's own page: nav, and seven panels, each saying what it is |
| `GET /ui/{path}` | that page's own shell assets. Not a contract; rearrange at will |
| `GET /elements/{path}` | **a contract.** `/elements/statemachined.js` registers `<statemachined-device>`, `-lines`, `-graph`, `-session`, `-trace` and `-firmware`, each with a shadow root and a `base` attribute |

**The UI uses only the API above.** There is no private route, which is what
makes the page an honest test of this document rather than a second, friendlier
interface to the same daemon — and it is checked rather than asserted:
`tests/unit/test_web_user_interface_routes.py` fails if the UI names a path this
daemon does not route.

**Nothing under either prefix is cached.** One daemon serves the elements and
the API they call, and that is what keeps them the same version; a browser
holding yesterday's element against today's API would give the guarantee away
for a few kilobytes.

A path is refused unless it is a file the UI actually contains, with a suffix
this daemon serves. It is the only place in the daemon that turns a URL into a
filesystem path, and the daemon runs where a config file and a graph store are.
