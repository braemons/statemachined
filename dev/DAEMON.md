# statemachined — the daemon

> **Status:** plan. Nothing here is built. It is the concrete shape of
> [`PLAN.md`](PLAN.md)'s **M4**, which that document leaves as one line — *"bridge
> to triald"* — and an empty `bridge/` directory. It also answers two of
> PLAN.md's open questions and contradicts one of its statements; both are
> marked below. Argue with it before any of it is written.

`PLAN.md` describes a *bridge*: a host process that translates between triald's
HTTP and the device's serial link. This document describes something larger and
says why. The bridge is still in here — it is `device/` plus `triald.py`, maybe
a fifth of the code — but a translator alone cannot be installed on a rig,
cannot be told which pin is the left lever, and cannot be asked what it is
running. The thing a rig needs is a **daemon**: a systemd unit that owns one
MCU, holds the graphs, and answers for both.

```
        ┌──────────────────────────────────────────────┐
        │  braemons-console   (separate repo, later)   │
        │  mDNS discovery · one screen · no logic      │
        └───┬───────────────┬───────────────┬──────────┘
            │ custom elements, loaded from each daemon
   ┌────────┴─────┐  ┌──────┴──────┐  ┌─────┴──────────┐
   │   vstimd     │  │   triald    │  │ statemachined  │   all on one Pi 5
   │  React/gRPC  │  │ vanilla/REST│  │  vanilla/REST  │
   └──────────────┘  └──────┬──────┘  └─────┬──────────┘
                            │  configure    │
                            │  ─────────▶   │  HTTP+JSON, the slow bus
                            │  ◀─────────   │
                            │   outcome     │ USB CDC · NDJSON · CRC
                                            │
                                    ┌───────┴────────┐
                                    │  Uno R4 Minima │  the timing authority
                                    └────────────────┘
```

---

## 1. What the daemon is for

Four jobs, and the first is the only one `PLAN.md` names.

| | |
|---|---|
| **Translate** | triald's HTTP ⇄ the device's NDJSON. Retry, CRC, reconnection, the session seed, clock correlation |
| **Compile** | a graph authored in *names* into the wire's *indices*. `PROTOCOL.md` puts names host-side deliberately; this is where host-side is |
| **Hold** | the graphs, the line map, the rig's config. A graph outlives a reboot and a reflash |
| **Answer** | what board is attached, what firmware it runs, what each line is wired to, what the machine is doing right now — over an API and a web UI |

The second and fourth are what make it worth a daemon rather than a library, and
the fourth is what makes the difference on a bench at 2 a.m.

### What it is emphatically not

**Not a second decision authority.** `PLAN.md` is explicit and it is right:
`precise_fixation` and `frame_loss` can each veto acceptance on their own, and
triald collects them — not this. The daemon reports what the device measured and
sets no veto field. See §4.3.

**Not the trial-type authority.** triald chooses the trial type and therefore
chooses the graph; the daemon is told which graph, by name, and never holds a
copy of the trial-type table. What the *wire* never carries is the trial type
itself — see §3.1, which is where the distinction actually matters.

**Not a timing authority.** Python on a Pi is nowhere near the 10 kHz scan. Every
number the daemon reports about a trial came off the device's clock.

---

## 2. Repository layout

`bridge/` becomes `daemon/`, `tools/bringup/` moves into it, and `packaging/`
appears. `firmware/` and `emulation/` do not move at all.

```
statemachined/
├── firmware/                  unchanged
├── emulation/                 unchanged
├── daemon/                    the Python daemon — was bridge/
│   ├── pyproject.toml            name = "statemachined", version sentinel 0.0.0
│   ├── LICENSE                   LGPL-3.0-or-later, moved from bridge/
│   ├── src/statemachined/
│   │   ├── cli.py                `statemachined serve`, and the bench commands
│   │   ├── config.py             /etc/braemons/statemachined.toml
│   │   ├── device/               everything that touches the wire
│   │   │   ├── link.py              pyserial transport        ← tools/bringup
│   │   │   ├── wire.py              framing, CRC              ← tools/bringup
│   │   │   ├── messages.py          MsgType/Field/ErrorCode   ← tools/bringup
│   │   │   ├── session.py           request/response, retry   ← tools/bringup
│   │   │   ├── upload.py            the chunked graph upload  ← tests/hardware/harness.py
│   │   │   ├── result.py            result reassembly         ← tests/hardware/harness.py
│   │   │   ├── supervisor.py        NEW: owns the port, reconnect, seed, watchdog
│   │   │   └── clock.py             NEW: device µs ⇄ host clock
│   │   ├── model/                pydantic — the graph as a person authors it
│   │   │   ├── graph.py             Graph, State, Transition, Action, Distribution
│   │   │   ├── lines.py             LineMap: names, pins, invert/enable/safe/debounce
│   │   │   ├── record.py            TrialResult and its path
│   │   │   └── outcome.py           the eleven .tdr codes
│   │   ├── compile.py            names → indices, and the caps check
│   │   ├── store.py              graphs on disk under /var/lib/statemachined
│   │   ├── triald.py             the client: POST /api/trial/outcome
│   │   ├── api/                  FastAPI routers — see §4
│   │   └── web/                  index.html · app.js · style.css · elements/
│   └── tests/
│       ├── unit/                 host-only. Runs in `make ci`
│       └── hardware/             needs a board    ← tools/bringup/tests/hardware
├── graphs/                    example graphs, authored against model/graph.py
├── packaging/                 see §6
└── tools/check-core-purity.sh stays. It is the only thing left in tools/
```

`tools/bringup/` disappears as a directory. Its README argues that the graph
upload and result reassembly in `tests/hardware/harness.py` are *"the bridge's
job and should move there when `bridge/` exists — at which point this suite
tests the bridge's codec against real hardware, which is strictly better than
testing a copy of it."* That is exactly what this move does, and it is the
strongest single argument for doing it first and separately (§7, M4a).

### What does not move: the second protocol implementation

`emulation/tests/statemachined_protocol.py` stays where it is and stays
independent. There are deliberately two implementations of `PROTOCOL.md` in this
tree — the firmware's and the emulator tests' — so that a test asks the device a
question it did not already know the answer to. The daemon importing the test's
framing (as `tools/bringup/wire.py` does today) was acceptable for a bench tool
run from a checkout. It is **not** acceptable for an installed package: a `.deb`
has no `emulation/` directory.

So `daemon/src/statemachined/device/wire.py` becomes a real third
implementation. That is a cost, and it is the right one: the alternative is
shipping the test suite inside the daemon package. The two are kept honest by a
golden-vector test — a fixed set of lines with known CRCs, asserted by both.

---

## 3. The graph, as a person authors it

The wire speaks indices because the device has 32 KB. A person speaks names.
The compiler between them is the daemon's main reason to exist.

```jsonc
// graphs/go-nogo.json — authored, stored, and served in this form
{
  "name": "go-nogo",
  "entry": "Ready",
  "distributions": {
    "foreperiod": { "kind": "uniform", "a": 300, "b": 700 },
    "window":     { "kind": "fixed", "a": 1000 }
  },
  "states": [
    { "name": "Ready",
      "on_entry": [ { "line": "ready_lamp", "kind": "high" } ],
      "transitions": [
        { "when": { "all": ["start_switch"], "none": ["abort"] },
          "goto": "Foreperiod" } ] },
    { "name": "Foreperiod",
      "timeout": { "after": "foreperiod", "goto": "Window" },
      "transitions": [
        { "when": { "any": ["lever_left", "lever_right"] },
          "goto": "Early" } ] },
    { "name": "Hit", "outcome": "HIT",
      "on_entry": [ { "line": "valve", "kind": "pulse", "ms": 40 } ] }
  ]
}
```

`compile.py` turns that into `graph_begin` … `graph_end` per `PROTOCOL.md` §3.2:
states in declaration order, each state's transitions and actions immediately
after it (the ordering rule *is* the device's memory invariant), the shared
distribution pool flattened and indexed, `invert`/`enable`/`safe`/`debounce_ms`
taken from the line map, and the rolling `checksum` accumulated as it goes.

**Validation happens twice, on purpose.** The daemon runs `PROTOCOL.md`'s rules
host-side *and* checks the graph against the `caps` in `hello_ack` before sending
a byte — turning "refused at `graph_end`" into "refused before the upload
starts", with an error naming the state by name rather than by index. The device
validates again regardless; it does not trust the host, and a daemon bug must not
be able to commit a bad graph. `graph_version` is the daemon's, incremented on
every successful upload, and `configure` carries it so a graph edit that did not
land cannot leave the device confidently running the old paradigm.

> **This answers PLAN.md open question 2, "Who authors a graph?"** — *"A Python
> builder in the bridge is cheap; a visual editor in triald's web UI is not, and
> triald has a no-build-step, no-CDN rule."* The builder is `model/graph.py`, and
> the visual editor turns out to be affordable after all, because it lives here
> and obeys the same rule. See §5.

> **And open question 1, "Where do graphs live in triald?"** — neither of the two
> options offered. **They live in statemachined**, under
> `/var/lib/statemachined/graphs/`, addressed by name. A triald `TrialType`
> references one by name, the way sets are addressed by name. That keeps N trial
> types from carrying N copies of a paradigm, and it puts the graph next to the
> only process that can validate it against a real device's `caps`.

### 3.1 Trial type → graph, and where the map lives

**triald holds the map, as a `graph` name on `TrialType`.** Its own `PLAN.md`
argues this before we do: *"It is a state machine that configures itself from the
trial type… Choosing a trial type and choosing a state machine are one act. That
is why the trial type is the join key."* Selecting a trial type therefore selects
a graph, and triald is the only process that selects trial types.

It is a **name**, not an index, and that too is triald's own conclusion about the
field this replaces — `TrialType.time_sequence`, an integer today: *"edit
sequence 3 and every trial type pointing at it silently changes meaning. Sets
were cured by naming them; time sequences want the same cure."* A graph name is
that cure, and the store in `/var/lib/statemachined/graphs/` is what it points
into.

So three different things, and only the first is a real constraint:

| | |
|---|---|
| The **wire** never carries a trial type | `configure` names a *graph slot*, an index into an uploaded set. The moment firmware can branch on trial type it becomes paradigm-specific, and *"firmware stable while paradigms change"* — the property both plans exist to protect — is gone. A slot selects a **machine**; it does not describe a **condition**, and the device still cannot tell a go trial from a catch trial |
| The **daemon** never holds the trial-type table | A second copy would be a second selection authority, the same mistake as merging the veto fields |
| The **daemon** resolves name → slot | triald says `"go-nogo"`; the daemon knows it is slot 3 of the committed set, because the daemon built the set |

### 3.2 The graph set: upload once, switch by index

**The device holds every graph a session will use, and `configure` switches
between them by index.** No upload happens inside an ITI, ever.

This is a change to `PROTOCOL.md`, not to anyone's intent — that document
specifies a single committed graph, `configure` names a `graph_version` rather
than a slot, and the firmware built at M2/M3 holds exactly two graph buffers.
The intent predates the spec; the spec has to catch up. §3.3 lists what moves.

#### Why the naive version does not fit, and what does

Twenty independent `StateGraph`s is **52 KB** against the R4's 32 KB. Measured,
on the structs as they stand:

| | Device | R4 pool today |
|---|---|---|
| `State` | 9 B | 288 B (×32) |
| `Transition` | 16 B | 1 024 B (×64) |
| `OutputAction` | 4 B | 256 B (×64) |
| `RandomDistribution` | 24 B | 768 B (×32) |
| `InputConfig` | 72 B | — |
| **one `StateGraph`** | **≈ 2.6 KB** | |

So graphs share one set of pools, and it is **the mechanism the firmware already
uses one level down**: a `State` addresses its transitions and actions as
`(first_transition, transition_count)` slices of flat pools. A graph is the same
thing one level up, and costs three bytes:

```c
struct GraphEntry { StateIndex entry, first_state; uint8_t n_states; };
```

Twenty of those is 60 B. `InputConfig` stops being per-graph in the same move:
invert, enable and debounce describe **the wiring**, not the paradigm, so one
copy per device is both 72 bytes cheaper and more nearly true than what is there
now.

#### The ceiling is `uint8_t`, not RAM

`StateIndex`, `TransitionIndex`, `OutputActionIndex` and
`RandomDistributionIndex` are all `uint8_t` with `0xFF` as the sentinel. **255 is
a hard ceiling on each pool across the whole set**, and widening one is a type
change reaching every struct and the wire format. That, not the SRAM, is what
bounds the design. At twenty graphs it budgets:

| | Across the set | Per graph |
|---|---|---|
| States | 160 | 8 |
| Transitions | 240 | 12 |
| Output actions | 200 | 10 |
| Distributions | 48 | shared and reused |

≈ **7.5 KB for the whole set** — against ~11.6 KB unclaimed on the R4, and
better than today's 5.2 KB for *two* graphs. Teensy 4.1 and ESP32 have neither
problem; the R4 sets the design, as `PLAN.md` intends.

**No per-graph limit.** One graph may take thirty states if its neighbours are
small. `hello_ack` reports the pool sizes, and the daemon refuses an oversize set
before sending a byte, naming which pool overran and by how much — the same
"refused before the upload starts" the caps check already gives.

#### One set, not two: what is given up and why it is affordable

Two full sets is ~15 KB, which needs the framework's 8 KB heap reclaimed
(`BSP_CFG_HEAP_BYTES`, a patch to the Arduino variant the build has so far
avoided). So the device holds **one** set, marked invalid until `graphset_end`
commits it.

That gives up `PLAN.md`'s *"a failed upload leaves the device running the
paradigm it was already running"*. The replacement is weaker but safe: a failed
upload leaves **no** graph — outputs go to their safe levels, `has_graph` goes
false, and the daemon sees it on the next reply rather than inferring it. It
fails safe and it is detectable, where the failure double-buffering guarded
against was silent.

And it is affordable now for a reason that did not exist before this section:
**uploads happen between sessions.** The property was urgent when an upload could
land in an ITI mid-session; it is a re-upload at setup time now.

### 3.3 What this changes below the daemon

This is no longer a daemon-only plan. Named so the firmware work is visible
rather than discovered at M4b:

| | |
|---|---|
| `firmware/core/config.h` | pools resized; `kMaxGraphs`; the `uint8_t` ceiling documented where it is set, not here |
| `graph/state_graph.h` | `GraphEntry` table; `InputConfig` and `output_safe_levels` move out of the per-graph struct to device scope; `validate()` runs per graph over its own slice |
| `protocol/graph_builder.cpp` | builds a set, appending into shared pools; the commit flag |
| `protocol/host_link_session.cpp` | the upload state machine; `configure` resolves a slot |
| `dev/PROTOCOL.md` | §3.2 gains `graphset_begin`/`graphset_end` around the existing per-graph messages; §3.3 `configure` takes `graph` (slot) alongside a set-level `graph_version`; §4.1 `hello_ack` caps gain `max_graphs` and the pool sizes; §4.2 `armed` echoes the slot |
| `dev/PLAN.md` | the RAM table, the "two graphs" note in the M3 measurement, and M4 |

`proto` stays **1**. `PROTOCOL.md` set that precedent at M3 for the `seq` →
`message_id` rename with the argument that applies here unchanged: no bridge has
shipped, so there is no deployed peer to keep compatible.

### 3.4 Randomised durations: already the design, and a contradiction

Timings are drawn **on the device**, and every realised value comes back per
visited state. This needs no change — `result_path` entries carry `drawn_ms` at
position 3, *"reported so a random timing is evidence in the record and not
merely reproducible from the seed"*, and the measured `duration_us` at position
5. Position 3 is what was asked for; position 5 is what happened.

> **This contradicts triald's `PLAN.md`**, which says *"**triald rolls the random
> durations** … two participants have to agree on a randomised interval anyway,
> so somebody must roll it once — and triald already owns a seeded RNG recorded
> with the session."*
>
> Both are right about different intervals, and the line is **who else needs to
> know**. An interval vstimd and the MCU must both act within has to be rolled
> once upstream and pushed down as a `fixed` distribution through `patch` —
> triald's argument holds exactly there. An interval only the MCU can observe — a
> foreperiod, a response window, a hold — is drawn on-device, because shipping it
> down would cost a round-trip on the slow bus for a number nobody else uses.
> Needs an amendment to say which, rather than each document assuming its own.

---

## 4. The API

FastAPI + pydantic. Every model is a transcription of something already
specified — `PROTOCOL.md` for the device surface, triald's `dev/API.md` for the
outbound `OutcomeReport`. Documented in `dev/API.md` here, generated schema at
`/openapi.json`, and the web UI uses **only** this API — no private route,
which is what keeps the UI an honest test of it.

### 4.1 Device and lines

| | |
|---|---|
| `GET /api/device` | connected, board, `fw`, `proto`, measured `scan_hz`, `caps`, `has_graph`, `graph_version`, link counters, uptime |
| `GET /api/device/lines` | per line: index, direction, pin label, **your name for it**, invert, enable, safe level, debounce, and its live level |
| `PATCH /api/device/lines` | rename a line; change invert/enable/safe/debounce |
| `GET /api/device/firmware` | the running version against what the installed package ships. See §6.3 |

**A caveat that shapes the UI.** `invert`, `enable`, `safe` and `debounce_ms` are
fields of `graph_begin` — the protocol has no standalone command for them. So
changing one is a **re-upload of the current graph**, and the API says so rather
than pretending it is a live setting. Renaming a line is free: names are the
daemon's alone and never reach the wire.

### 4.2 Graphs

| | |
|---|---|
| `GET /api/graphs`, `GET·PUT·DELETE /api/graphs/{name}` | the store |
| `POST /api/graphs/{name}/validate` | every rule, plus this device's `caps`. Changes nothing |
| `POST /api/graphs/set` | compile a named list into one set, check it against the pool budget, upload, commit. → the set's `graph_version` and the slot each name got |
| `GET /api/graphs/set` | what is committed on the device right now, name by slot |

### 4.3 The trial loop

triald drives; statemachined reports.

| | |
|---|---|
| `POST /api/trial/configure` | `{trial_id, graph, cap_ms, start, patch}` → armed. **`graph` is a name**; the daemon resolves it to a slot in the committed set and arms. No upload. See below |
| `POST /api/trial/start` | when `start` admits serial |
| `POST /api/trial/cancel` | `{trial_id, reason:"host"}` |
| `GET /api/state` · `WS /api/stream` | snapshot, and coalesced frames — triald's convention, and for the same reason: a slow browser tab must not hold up a session |

**`configure` never uploads.** By §3.2 the set is already on the device, so this
is a name lookup and one `configure` on the wire — bounded, and the same cost for
every trial in a session whatever its graph. A `graph` naming something outside
the committed set is **refused**, not uploaded on demand: an upload mid-session
is exactly what the set exists to prevent, and silently doing one would hide a
misconfigured trial type behind a trial that armed late.

The set is therefore a **session-scoped** object. `POST /api/graphs/set`
assembles the graphs a session needs, validates the whole thing against the pool
budget, uploads and commits — at setup, from the UI or from triald. Which graphs
go in it is a question the daemon cannot answer on its own: it is the union of
what the loaded sets' trial types reference, which only triald knows. Two ways to
supply it, and both should work:

| | |
|---|---|
| **triald declares it** | `POST /api/graphs/set {graphs: ["go-nogo", "catch", …]}` when a session starts. Explicit, and triald already knows the answer |
| **the daemon is told the names** and refuses at session start if any is missing, rather than at trial 40 | The check belongs before an animal is in the booth |

Outbound, one call: `POST {triald}/api/trial/outcome` with an `OutcomeReport`
carrying `outcome`, `manipulandum`, `reaction_time_ms`, `terminating_interval`,
`reward_ms`, `simulated: false`. **`precise_fixation` and `frame_loss` are left
at their defaults** — the daemon has never heard of the eye monitor or vstimd,
and acquiring an opinion about them would make it a second decision authority.

A cancel that races a terminal state comes back with the **real outcome**, not a
fabricated `CANCELLED`. The daemon passes that through unchanged; asking to
cancel and being told `HIT` is triald's to cope with, and the alternative is a
record claiming a trial was cancelled when the animal had already responded.

> **This contradicts triald's `dev/API.md`.** That document says of the trial
> loop: *"**Pull, not push.** The caller asks for a trial when it is ready, which
> keeps triald reactive and stops it becoming the session's clock."* Under the
> arrangement above, triald calls `configure`/`start` here — which makes triald
> the clock, the precise thing it declined to be. It is still the right split
> (triald knows the ITI, the set and the switch rule; statemachined knows only
> one device), but it needs **an amendment to triald's API.md, not a silent
> divergence** — the same standard `PLAN.md` §"What this changes in triald" sets.
> Flagging it; not resolving it here.

### 4.4 Config

`GET·PATCH /api/config`: the device target URL, the triald base URL, the seed
policy, the line map, whether to arm automatically on connect. Backed by
`/etc/braemons/statemachined.toml`.

### 4.5 The clock

`PLAN.md` calls this load-bearing and it is worth restating why. The device
timestamps in its own microseconds, which wrap every ~71 minutes and are not
comparable between trials. triald can only ask vstimd *"was there frame loss
during trial 42"* if trial 42's window arrives in a clock vstimd shares. Hand
over raw device microseconds and the question cannot be asked at all.

`device/clock.py` estimates the offset from `ping` round-trips and reports every
trial window in the host clock alongside the raw device values. The raw values
are never discarded — they are the evidence; the correlation is an estimate and
is labelled as one, with its uncertainty.

---

## 5. The web UI

No build step, no framework, no CDN — triald's rule, and for its reason: a rig
box may have no route to the internet and a browser in a booth must not wait on
unpkg. Everything is `index.html`, `app.js`, `style.css` and `elements/`, served
by the daemon as package data.

**Every view is a custom element with a shadow root.** That is the one decision
here that would be expensive to retrofit, and it costs nothing now:

```html
<script type="module" src="http://rig.local:8081/elements/statemachined.js"></script>
<statemachined-device base="http://rig.local:8081"></statemachined-device>
<statemachined-lines  base="http://rig.local:8081"></statemachined-lines>
<statemachined-graph  base="http://rig.local:8081" name="go-nogo"></statemachined-graph>
```

Three consequences, each of them the point:

- **Shadow DOM makes embedding safe.** triald's `style.css` is ~600 lines of
  global selectors (`.card`, `.col`, `.banner`). Unshadowed markup dropped into
  that page is a class-collision hunt forever; a shadow root cannot be reached by
  it, natively, with no tooling.
- **Custom elements are the only interop layer the three UIs share.** vstimd is
  React 18 + Vite + protobuf; triald is vanilla; this is vanilla. React renders
  `<statemachined-lines>` fine — every prop here is a string, so even React 18's
  attribute-only support suffices. A shared *framework* would mean triald grows a
  Vite build or vstimd loses one: real work, no gain.
- **The element and the API it calls are always the same version**, because one
  daemon serves both. A console that bundled a copy of this UI would drift the
  first time a field changed.

The price is CORS on `/api/` and `/elements/`, and treating `/elements/` as a
public contract. `base` is an attribute because nothing may assume same-origin.

**mDNS.** vstimd already advertises `_vstimd._tcp` on 5555 via
`packaging/avahi/vstimd.service.tmpl`, with a stable `id=` TXT record *"so
clients can match a device across name collisions"*. statemachined advertises
`_statemachined._tcp` the same way, so a console discovers the rig instead of
being hand-configured with URLs — which matters on a Pi whose hostname is
generated at boot.

### Views

| | |
|---|---|
| **Device** | board, link health, firmware, and the live `in`/`out` bit rows — the thing `statemachined-bringup state` prints today, but named and updating |
| **Lines** | the map. Rename, invert, enable, safe level, debounce. Shows which need a re-upload to take effect |
| **Graphs** | the store, and the editor: states, timeouts, terminal outcomes, actions, and a predicate editor where the three masks are checkboxes over *named* lines. An SVG node diagram rendered from the graph, read-only in v1 |
| **Session** | the current trial, the state the machine is in, the last result's path |
| **Firmware** | running against available; the mismatch warning |

### The console

A **separate repo** (`braemons-console`), not this branch. A small static shell:
mDNS discovery, nav, layout, rig-wide status — and no domain logic at all, since
every panel is an element served by the daemon that owns it. Building it here
would make this branch depend on two other repos' UIs before it could ship
anything. What this branch owes it is the `/elements/` contract and the mDNS
record, both above.

---

## 6. Packaging

The braemons pattern for a Python daemon is already written down, in
`triald/packaging/README.md`, and is a skeleton there too. **statemachined would
be the first Python braemons daemon to actually build packages**, and triald
inherits whatever this gets right.

### 6.1 Shape

- **Vendored interpreter.** `uv` plus python-build-standalone build a
  self-contained tree at `/opt/braemons/statemachined`, so the artifact does not
  depend on whatever Python the distribution ships and behaves like a compiled
  binary. This is why the packages are per-architecture despite being pure
  Python.
- **One `nfpm` config → both formats.** `.deb` for amd64/arm64, `.rpm` for
  x86_64/aarch64. Simpler than vstimd's split, which needs `cargo-deb` for Debian
  and a hand-written `.spec` for RPM.
- **Dependencies are small**: fastapi, uvicorn, pydantic, pydantic-settings,
  pyserial, httpx. Nothing like triald's numpy/scipy problem.
- **Version from the git tag.** `packaging/scripts/git-version.sh`, lifted
  verbatim from triald/vstimd; `pyproject.toml` carries the `0.0.0` sentinel, so
  a `0.0.0` artifact means the stamping was bypassed.

### 6.2 Paths

| | |
|---|---|
| `/opt/braemons/statemachined/` | the vendored interpreter and the package |
| `/etc/braemons/statemachined.toml` | conffile: device target, triald URL, line map |
| `/var/lib/statemachined/graphs/` | the graph store |
| `/var/log/statemachined/` | logs, rotated weekly |
| `/usr/share/braemons/statemachined/firmware/` | the flashable images and `MANIFEST.txt` |

Runs as its own unprivileged user via sysusers, with the same systemd hardening
triald's unit uses (`ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
`NoNewPrivileges`, `StateDirectory`).

**The device node needs a udev rule, not a group.** Adding the daemon user to
`dialout` (Debian) or `uucp` (Fedora) grants it every serial device on the box
and differs per distro. Instead `packaging/udev/60-statemachined.rules` matches
the board's VID/PID, grants that node to the `statemachined` user, and creates a
stable symlink `/dev/braemons/statemachined0`. That also solves *which port* on a
Pi with several USB devices, which `/dev/ttyACM0` does not.

### 6.3 Firmware in the package

`make image` already builds both flashable images with a `MANIFEST.txt`
recording the commit, sizes and checksums — because *"a board in a rack cannot be
asked which commit it is running."* The package installs that under
`/usr/share/braemons/statemachined/firmware/`, and `GET /api/device/firmware`
compares the running `fw` from `hello_ack` against it and warns on a mismatch.

Flashing itself is deferred. When it lands it is a separate optional package
pulling in `bossac`/`dfu-util`, because it means dropping the port, flashing, and
waiting for re-enumeration mid-session — which is a different risk from anything
else the daemon does.

### 6.4 Release and the archive

The tag-driven `release.yml` builds the packages *and* `make image`, and attaches
both to the GitHub Release: `.deb` ×2, `.rpm` ×2, the bench and rig images, and
`MANIFEST.txt`. A hyphen in the tag marks a pre-release, as in vstimd.

Ingestion into the archive is **one line** in
`braemons/packages/sources.txt`, which currently reads only `braemons/vstimd`:

```
braemons/statemachined
```

That is the whole integration — this repo needs no workflow changes for it and
holds no credentials. A `~` in the version routes a pre-release to `testing`
rather than `stable` automatically.

---

## 7. Milestones

Each is a branch that leaves the tree working. `PLAN.md`'s M4 is replaced by
M4a–M4e; its M5–M7 shift down and need renumbering in that document.

| | |
|---|---|
| **M4a** | **The move, and nothing else.** `tools/bringup/` → `daemon/`, package renamed, `wire.py` made standalone with golden vectors against the emulator's copy. `make bringup` and `make test-hardware` keep working, unchanged in behaviour. Reviewable as a pure move |
| **M4b′** | **The graph set, in the firmware** (§3.2, §3.3). `PROTOCOL.md` amended first, then `config.h`, `state_graph.h`, `graph_builder`, `host_link_session`; host tests and the Renode session updated. Reopens M2/M3 work and is the one milestone here that is not Python. Ends with the RAM figure re-measured on a board |
| **M4b** | `model/` and `compile.py`: the pydantic graph, the line map, names → wire, and the set with its pool budget. Host tests against the amended `PROTOCOL.md` §3.2 message by message. `graphs/` gets go/no-go and 2AFC, which fills the directory `PLAN.md` has had empty since M0 — **and is what tests open question 4** |
| **M4c** | `device/supervisor.py` and `clock.py`: owns the port, reconnects, holds the seed, arms the watchdog, reassembles results. Integration-tested against the native core over a pty — whole trials, cancel races, link loss, as `PLAN.md` §Testing asks |
| **M4d** | FastAPI: device, lines, graphs, trial, config, state/stream; the triald client; `statemachined serve`. `dev/API.md` written first, the way `PROTOCOL.md` was |
| **M4e** | The web UI and the `/elements/` contract; mDNS |
| **M5** | Packaging: nfpm, systemd, sysusers, udev, logrotate, the builder containers, `release.yml`, one line in `packages/sources.txt`. **Installed on the Pi 5 alongside vstimd and triald** |
| **M6** | A whole session on the R4 with `triald sim`'s simulated subject replaced by the real board — which is what `PLAN.md`'s M4 actually asked for, and it needs everything above |

**M4a is worth doing and merging on its own.** It is a move with no new
behaviour, it makes the hardware suite test the daemon's codec instead of a copy
of it, and every milestone after it is easier to review for having it out of the
diff.

---

## 8. Open questions

1. **The bench CLI's name.** triald has `triald` + `trialctl`; vstimd has one
   binary. `statemachined serve` is the daemon — is the bench client
   `statemachined <cmd>`, or a separate `smctl`? The bringup README argues hard
   that the bench tool *"is not the bridge"*, and folding it into the daemon
   binary blurs that. Leaning: keep them one binary, keep the subcommands
   grouped, and let the README's rule live in the code's structure rather than in
   two entry points.
2. **One device per daemon.** v1 assumes it: one target in the config, `/api/device`
   singular. A rig with two MCUs is then `statemachined@.service`, a systemd
   template with a config per instance. Confirm that is far enough off to defer.
3. **triald's "pull, not push".** §4.3 flags a genuine contradiction with
   triald's `dev/API.md`. Someone has to decide which document changes.
4. **The pool budget at twenty graphs** (§3.2). 160 states / 240 transitions /
   200 actions is arithmetic against a `uint8_t` ceiling, not against real
   paradigms. Authoring the `graphs/` examples at M4b is what tests it, and it is
   cheap to find out then. If twelve transitions per graph proves too few, the
   answer is a `uint16_t` index — a type change through every struct and the wire
   format, and much better decided at M4b than at M6.
   **This is the question most likely to change the plan.**
5. **`TrialType.graph` is triald's field to add**, alongside or replacing
   `time_sequence`. Same amendment as #3, and probably the same patch.
6. **Global timers** (`PLAN.md` open question 3, still open) are the one feature
   likely to change `model/graph.py`'s shape. Not in v1, but the model should be
   written so they are an addition rather than a rewrite.
7. **Who authors the console**, and when. §5 defers it; it should not stay
   deferred long, since it is most of what makes three daemons feel like one rig.
