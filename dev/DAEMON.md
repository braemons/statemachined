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

Five jobs, and the first is the only one `PLAN.md` names.

| | |
|---|---|
| **Translate** | triald's HTTP ⇄ the device's NDJSON. Retry, CRC, reconnection, the session seed, clock correlation |
| **Compile** | a graph authored in *names* into the wire's *indices*. `PROTOCOL.md` puts names host-side deliberately; this is where host-side is |
| **Hold** | the graphs, the line map, the rig's config. A graph outlives a reboot and a reflash |
| **Answer** | what board is attached, what firmware it runs, what each line is wired to, what the machine is doing right now — over an API and a web UI |
| **Trace** | a timestamped log of every state the machine entered, keyed by `trial_id`, kept whether or not anybody asked for it. §3.6 and §4.6 |

The second and fourth are what make it worth a daemon rather than a library, the
fourth is what makes the difference on a bench at 2 a.m., and the fifth is the
one job that is *only* possible in a daemon: it has to be running and listening
at the moment a state is entered, which no library invoked per trial is.

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
├── firmware/                  unchanged, apart from native/ below
│   └── native/                the firmware on this machine, for integration tests
├── emulation/                 unchanged
├── daemon/                    the Python daemon — was bridge/
│   ├── pyproject.toml            name = "statemachined", version sentinel 0.0.0
│   ├── LICENSE                   LGPL-3.0-or-later, moved from bridge/
│   ├── src/statemachined/
│   │   ├── command_line_interface.py   `statemachined serve`, and the bench commands
│   │   ├── board_pin_labels.py         pin labels per board, for what the CLI prints
│   │   ├── daemon_configuration.py     /etc/braemons/statemachined.toml
│   │   ├── device/                     everything that touches the wire
│   │   │   ├── serial_link.py                pyserial transport   ← tools/bringup
│   │   │   ├── message_framing.py            framing, CRC         ← tools/bringup
│   │   │   ├── message_vocabulary.py         MsgType/Field/Error  ← tools/bringup
│   │   │   ├── request_response_session.py   one in flight, retry ← tools/bringup
│   │   │   ├── graph_set_upload.py           the chunked set upload
│   │   │   ├── trial_result_reassembly.py    result reassembly
│   │   │   ├── device_supervisor.py          NEW: owns the port, reconnect, seed, watchdog
│   │   │   ├── device_clock_correlation.py   NEW: device µs ⇄ host clock
│   │   │   └── state_visit_trace.py          NEW: the visit ring, and the NDJSON tail
│   │   ├── model/                      pydantic — the graph as a person authors it
│   │   │   ├── graph_definition.py     Graph, State, Transition, Action, Distribution
│   │   │   ├── line_map.py             names, pins, invert/enable/safe/debounce
│   │   │   ├── trial_record.py         a result and its path, read back into names
│   │   │   └── trial_outcome.py        the eleven .tdr codes
│   │   ├── graph_set_compiler.py       names → indices, and the caps check
│   │   ├── graph_store.py              graphs on disk under /var/lib/statemachined
│   │   ├── triald_client.py            POST /api/trial/outcome
│   │   ├── api/                        FastAPI routers — see §4
│   │   └── web/                        index.html · app.js · style.css · elements/
│   └── tests/
│       ├── unit/                 host-only. Runs in `make ci`
│       ├── integration/          whole sessions against the native device
│       └── hardware/             needs a board    ← tools/bringup/tests/hardware
├── graphs/                    example graphs, and the reference rig's line map
├── packaging/                 see §6
└── tools/check-core-purity.sh stays. It is the only thing left in tools/
```

**The file names are long on purpose.** A module called `compile.py` tells a
reader nothing about what it compiles or into what, and this tree has three
things that could plausibly be called a graph. So a file says what it is:
`graph_set_compiler.py` compiles a graph set, `trial_result_reassembly.py`
reassembles a trial result, `message_framing.py` frames messages. The cost is
paid at the import line and once; the alternative is paid by every reader.

`tools/bringup/` disappears as a directory. Its README argued that the graph
upload and result reassembly in `tests/hardware/hardware_test_harness.py` are *"the bridge's
job and should move there when `bridge/` exists — at which point this suite
tests the bridge's codec against real hardware, which is strictly better than
testing a copy of it."* That is exactly what the move does, and it was the
strongest single argument for doing it first and separately (§7, M4a, done).

### What does not move: the second protocol implementation

`emulation/tests/statemachined_protocol.py` stays where it is and stays
independent. There are deliberately two implementations of `PROTOCOL.md` in this
tree — the firmware's and the emulator tests' — so that a test asks the device a
question it did not already know the answer to. The daemon importing the test's
framing (as `tools/bringup/wire.py` does today) was acceptable for a bench tool
run from a checkout. It is **not** acceptable for an installed package: a `.deb`
has no `emulation/` directory.

So `daemon/src/statemachined/device/message_framing.py` becomes a real third
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
    "foreperiod":      { "kind": "exponential", "minimum_ms": 500,
                         "maximum_ms": 2500, "mean_ms": 900 },
    "response_window": { "kind": "fixed", "duration_ms": 1000 }
  },
  "states": [
    { "name": "Ready",
      "on_entry": [ { "line": "ready_lamp", "kind": "high" } ],
      "transitions": [
        { "when": { "all": ["start_switch"], "none": ["abort"] },
          "goto": "Foreperiod" } ] },
    { "name": "Foreperiod",
      "timeout": { "after": "foreperiod", "goto": "Cue" },
      "transitions": [
        { "when": { "any": ["lever_left", "lever_right"] },
          "goto": "Early" } ] },
    { "name": "Hit", "outcome": "HIT",
      "on_entry": [ { "line": "reward_valve", "kind": "pulse", "pulse_ms": 40 } ] }
  ]
}
```

**The parameters are named, where the wire's are positional.** A distribution on
the wire carries `a`, `b` and `c`, whose meaning depends on `kind` — terse
because the device has 32 KB. A person writing a foreperiod down should write
`minimum_ms` and `mean_ms`, and exactly one place in the daemon should know
which is which. That place is `graph_set_compiler.py`.

`graph_set_compiler.py` turns the whole set into `set_begin` … `set_end` per `PROTOCOL.md`
§3.2: the shared distribution pool first, then each graph as `graph_begin` …
`graph_end` with its states in declaration order and each state's transitions
and actions immediately after it (the ordering rule *is* the device's memory
invariant). It produces a *plan* — an ordered list of messages — and touches no
link, which is what lets the translation be tested message by message on a host
with no board. The rolling `checksum` is accumulated by the uploader, over the
bytes it actually sent.

Two distributions that are written identically become **one pool entry**, shared
across the set. Thirty-two entries is the scarcest thing on the board, two
paradigms usually want the same foreperiod, and anything that differs in a
parameter is a separate entry — so sharing can never change what a graph draws.

**Validation happens twice, on purpose.** The daemon runs `PROTOCOL.md`'s rules
host-side *and* checks the graph against the `caps` in `hello_ack` before sending
a byte — turning "refused at `graph_end`" into "refused before the upload
starts", with an error naming the state by name rather than by index. The device
validates again regardless; it does not trust the host, and a daemon bug must not
be able to commit a bad graph. `set_version` is the daemon's, incremented on
every successful set upload, and `configure` carries it so a graph edit that did
not land cannot leave the device confidently running the old paradigm.

> **This answers PLAN.md open question 2, "Who authors a graph?"** — *"A Python
> builder in the bridge is cheap; a visual editor in triald's web UI is not, and
> triald has a no-build-step, no-CDN rule."* The builder is `model/graph_definition.py`, and
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

### 3.2 Switching graphs: upload the set once, then switch by index

**Every graph a session uses goes to the device before the session starts, and a
trial selects one by index.** `POST /api/session/graphs` already exists to
declare the names a session will use and check them against the device's `caps`
before an animal is in the booth; it becomes the call that *uploads* them too,
and commits the lot as one set. `configure` then carries a slot, so changing
paradigm between two trials costs one field on a message the device was going to
receive anyway. No upload between trials, and nothing added to the ITI.

#### Why not upload per trial

Uploading on `configure` needs no firmware change at all, and it is cheaper than
an earlier draft of this section claimed. That draft reasoned from 115 200 baud
and got about a second, which is wrong on the reference board: the link is USB
CDC, where the baud rate is nominal and the transport is 12 Mbit/s full-speed
USB. Throughput was never the constraint. The real cost is **per command**, and
commit `79d50e4` measured it on hardware while chasing a climbing overrun count:

| | |
|---|---|
| draining the link (TinyUSB) | 1240 µs |
| reading the link (TinyUSB) | 754 µs |
| `tud_task()`, via `link_up()` | 533 µs |
| `HostLinkSession` — ours | 372 µs |
| the scan itself | 7 µs |

≈ **2.5 ms per command, almost all of it vendor USB stack.** A ten-state graph is
roughly fifty messages, so ≈ 125 ms device-side, call it 200–400 ms once the
host's round-trips are counted — inside any ITI worth having.

What that number does not fix is *which* trials pay it. An upload that happens
only when the trial type **changes** makes the ITI systematically longer on
exactly those trials: a timing difference **correlated with the variable under
study**, which is a confound rather than an inconvenience. Every ordering triald
offers draws without replacement, so type changes are frequent and irregular.

It is fixable — the daemon reports what the switch cost, triald pads the ITI to a
fixed floor above it, and the animal sees no difference between a repeat and a
switch. But that is a mitigation two daemons have to keep honouring correctly, on
every rig, forever, against a confound that is invisible in the data when
somebody does not. **Uploading nothing removes it rather than budgeting for it**,
and that is the whole argument.

Two things come with it:

- **The failure moves out of the session.** A graph too big for the board is
  refused while the set is uploading, minutes before the first trial, instead of
  at trial 40. That was already the point of `POST /api/session/graphs`; making
  it the upload gives it teeth, because a set that validated is a set the device
  is holding.
- **It is the same answer on a UART rig.** On `uno_r4_minima_sci` the host is on
  SCI2 where 115 200 baud is real and one graph is ~1.3 s of traffic. Per trial
  that is disqualifying; once per session it is a progress bar.

#### What it costs: one set of pools

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

So graphs share one set of pools — **the mechanism the firmware already uses one
level down**, where a `State` addresses its transitions and actions as `(first,
count)` slices. A graph is the same thing one level up, at three bytes:

```c
struct GraphEntry { StateIndex entry, first_state; uint8_t n_states; };
```

Twenty of those is 60 B. `InputConfig` stops being per-graph in the same move:
invert, enable and debounce describe **the wiring**, not the paradigm, so one
copy per device is both cheaper and more nearly true than what is there now —
which is §3.3's change and §3.4's fail-safe fix, arrived at from a third
direction.

**The ceiling is `uint8_t`, not RAM.** `StateIndex`, `TransitionIndex`,
`OutputActionIndex` and `RandomDistributionIndex` all use `0xFF` as a sentinel,
so 255 is a hard limit per pool across the whole set, and widening one is a type
change reaching every struct and the wire format. Twenty graphs would budget ~160
states, ~240 transitions, ~200 actions — about **8 states and 12 transitions
each**, in ≈ 7.5 KB against ~11.6 KB unclaimed.

That per-graph figure is an average and not a rule. The pools are shared, so a
session using four graphs gets forty states each, and **twenty is this
document's example rather than a number in the design**: `caps` gains
`max_graphs`, the daemon checks the *summed* usage of the set it is about to
upload, and nothing in the daemon hard-codes either figure.

**The set is single-buffered, and that is the one real regression.** Two sets do
not fit without reclaiming the framework's 8 KB heap, so the set commits in
place: a failed or abandoned upload leaves the device holding **no** graph rather
than the previous one. Today's double-buffering exists precisely to protect a
running paradigm from a bad upload, and with a set there is nothing left to
protect it with. What makes that acceptable is that it fails safe and loudly —
no graph means every output at its safe level, and §3.4's wiring is what makes
that mean the right thing rather than "all low" — and that it can only happen
between sessions, since a set upload is refused with `busy` while a trial is
armed or running, exactly as a graph upload is today.

Teensy 4.1 and ESP32 have none of these problems. The R4 sets the design, as
`PLAN.md` intends.

#### The escape hatch is the design this replaces

A session whose graphs will not fit the pools is not stuck. A **set of one**,
re-uploaded on `configure`, is the same mechanism and the same messages, and it
costs exactly the ITI padding described above — so the per-trial design survives
as a mode rather than as dead reasoning. It is also what a bench session and a
demo already do, which is reason enough to keep it working.

It is an explicit choice and not a silent fallback: `graph_mode = "set"` (the
default) or `"per_trial"` in §4.4's config. Falling back automatically would
mean a session quietly acquiring an ITI confound because somebody added a state,
which is the failure this section is built to avoid. The daemon holds the graphs
and the `caps`, so `POST /api/session/graphs` can say the set does not fit and
name what pushed it over — at which point shrinking a graph and switching the
mode are both informed decisions.

#### The variant not to reach for

Uploading *during* a trial, into the staging buffer, and committing when the
trial ends. It is refused today (`host_link_session.cpp:251`, `busy` / `"a trial
is armed or running"` / context `"graph upload"`), and it should stay refused: it
trades ITI time for ~2.5 ms of USB stack, fifty times, **during the trial being
timed**, and jitter on the trial you are measuring is a worse failure than a
longer ITI and harder to see. It was an answer to a per-trial upload cost that no
longer exists.

#### What is left to measure

`graph_switch_ms` was the number this section used to turn on, and by
construction it is now the cost of one extra field. What replaces it is duller
and belongs in `HARDWARE.md` next to the scan rate: the wall-clock cost of
uploading a whole set at session start, per transport. It bounds how long
`POST /api/session/graphs` blocks, and on SCI2 it is tens of seconds, which is a
progress bar in the UI rather than a design problem. Answerable the day M4e can
upload against a board.

### 3.3 What this changes below the daemon

**Two firmware changes, and the graph set is the larger of them.** M2 and M3 do
not stay closed: §3.2 asks the device to hold a set rather than a graph, which is
shared pools, a `GraphEntry` table, a set-shaped upload and a slot in
`configure`. That is `config.h`, `graph/state_graph.h`, `protocol/graph_builder.cpp`,
`protocol/host_link_session.cpp`, `PROTOCOL.md` §3.2/§3.3/§4.1/§4.2, and the
Renode session — **M4c** in §7, a milestone of its own and not a Python one.

On the wire it is four additions and one rename:

| | |
|---|---|
| `set_begin` / `set_end` | wrap N graph uploads. `set_end` validates the whole set and commits it; nothing is committed before it, and there is no partial set |
| `graph_begin` gains `slot` | which index in the set this graph is. Stated rather than implied by arrival order, for the reason `graph_state.i` already is |
| `configure` gains `graph_index` | `u8`, the slot. This is the switch |
| `graph_version` → `set_version` | it identifies the committed **set** now. `configure` still checks it, for the same reason: a set edit that did not land would leave the device confidently running the old paradigm |
| `hello_ack` | `has_graph` → `has_set` plus `n_graphs`; `caps` gains `max_graphs` |

`patch` indices become set-global, since the distribution pool is. That is the
daemon's problem and not triald's — triald patches by name, as it already does.

`proto` stays **1**, on the precedent `PROTOCOL.md` set at M3 for the `seq` →
`message_id` rename: no bridge has shipped, so there is no deployed peer to keep
compatible.

**The second change is smaller, and §3.4 turns it from tidiness into a fail-safe
fix**: move `InputConfig` and `output_safe_levels` out of `StateGraph` to
device scope. They describe the wiring rather than the
paradigm; re-sending them with every graph is what makes "change the debounce"
read as "re-upload the graph" in §4.1, and keeping them only inside a graph is
why a board with no graph cannot fail safe correctly.

§3.2 needs it too, since a shared pool set has no per-graph place to put it. It
is **M4b** in §7. **Persisting it is deferred** — see the end of §3.4 — so M4b
takes the half that needs no flash driver:

| | |
|---|---|
| `graph/state_graph.h` | `InputConfig` and `output_safe_levels` move out to device scope |
| `protocol/host_link_session.cpp` | a `wiring` command, refused with `busy` during a trial |
| `src/main.cpp` | the compile-time safe levels of §3.4, applied before the first `fail_safe()` |
| `dev/PROTOCOL.md` | §3 gains `wiring`; §4.1 `hello_ack` says whether the board has been given one, so the daemon never guesses whether it came up configured |

### 3.4 Data flash: what a board should know before anybody greets it

**A rig image boots into nothing, and drives every output low doing it.** Demo
mode is compile-time — `STATEMACHINED_DEMO`, 1 by default, and `make
firmware-rig` builds it out, which is why CI publishes a bench image and a rig
image. The rig image has no graph at reset, so `main.cpp`:

```c
// Every line to its safe level before the first scan. With no graph yet that
// is all low, and it is applied again the moment a graph is committed.
apply(g_session->fail_safe());
```

`output_safe_levels` lives inside `StateGraph`. `fail_safe()`'s own comment says
*"'off' is not always 'low', so this is data rather than a zeroed word"* — and at
reset there is no data, so the device cannot honour the rule at the one moment it
matters most. **An active-low valve driver is opened by every power cycle** until
the daemon connects. That is a fail-safe hole, not an inconvenience, and it is
the concrete reason to make the move §3.3 already argues for on tidiness grounds.

The RA4M1 has **8 KB of data flash**, separate from the 256 KB of code flash and
rated far higher — order 100 000 erase cycles against code flash's ~1 000
(confirm against the datasheet before it is load-bearing). Nothing in the tree
touches it today: there is no EEPROM, NVM or persistence of any kind. Two things
go in it.

**The wiring config, written when a rig is wired.** Invert, enable, debounce and
the safe levels — ~80 B, changed when somebody re-wires the box and essentially
never otherwise. This is what closes the hole above: the board reads it before
the first scan and drives the pins the rig actually needs, with no host in the
picture. Endurance is a non-question at this write rate.

**The committed graph, on explicit request.** ~2.6 KB, so it fits with room to
spare, and it means a board survives a power blip still running its paradigm
rather than waiting on a reconnect.

> **It must never be automatic on `set_end`.** Under §3.2 a set is uploaded once
> per session, which sounds harmless — but it is also uploaded on every graph
> edit, and a paradigm under development is edited dozens of times an afternoon
> by somebody watching the UI. Persisting each one would spend the part's
> ~100 000 erase cycles on drafts, silently, presenting years later as a board
> that stops accepting graphs. So it is a distinct command the daemon issues
> deliberately (at session setup, or from the UI), and `set_end` never writes
> flash on its own.

**No flash write while a trial is in flight.** `PLAN.md` already names this
hazard for the ESP32 — *"be careful of flash operations stalling interrupts"* —
and it is the same class on the RA4M1: a data-flash write blocks the flash
controller and can cost scans. Refused with `busy`, like the graph upload at
`host_link_session.cpp:251`, and for the same reason.

What this adds below the daemon: a `persist` message in `PROTOCOL.md` §3, a
`hal::` entry point for the data flash (the HAL is *"seven functions"* and this
makes it eight, on a boundary that already exists), a boot-time read in
`main.cpp` before `fail_safe()`, and `hello_ack` reporting what was restored so
the daemon never has to guess whether the board came up configured.

#### Deferred, and what holds the hole shut meanwhile

**The data flash itself is later work.** The `hal.h` addition, the RA4M1
implementation, the file-backed `native.cpp` stand-in, the boot-time read and the
`persist` command are a milestone of their own, after the daemon exists and can
exercise them; nothing above M6 needs them. Open questions 8 and 12 defer with
them.

That leaves the fail-safe hole open, so it should be shut cheaply rather than
left. Two things do it:

**A compile-time safe-level word in the rig image.** `make firmware-rig` already
builds a distinct binary; `-DSTATEMACHINED_SAFE_LEVELS=0x…` makes the first
`fail_safe()` correct for a rig whose wiring is fixed, which is every rig, and
costs one constant. It is worse than data flash in exactly one way — changing the
wiring means rebuilding rather than a `wiring` command — and that is the argument
for doing the flash later, not for leaving the outputs wrong now.

**The daemon pushes the wiring on connect**, before it does anything else, so the
window in which a board holds compile-time defaults is the seconds between power
and the first `hello`. With the constant in place that window is safe rather than
merely short, which is the point: the mitigation must not depend on the daemon
being up.

### 3.5 Randomised durations: already the design, and a contradiction

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

### 3.6 Transitions as they happen

The device already records every state it visits and reports the lot at the end
of a trial, chunked (`PROTOCOL.md` §4.3). That is the right *record* and it stays
authoritative. What it is not is a **trace**: something with a timestamp on it
that arrives while the trial is still running, that survives a truncated path,
and that another instrument's data can be aligned against months later.

So the device also emits one message per completed state visit, as it happens.

#### The message

```json
{"msg_type":"visit","message_id":57,"trial_id":193,"seq":2,
 "v":[1,"transition",2,0,500120,183044],"crc":"...."}
```

`v` is **the same six-element array** as a `result_path` entry —
`[state_index, exit_cause, transition_index, drawn_ms, entered_us, duration_us]`
— decoded by the same function on the host. Two shapes for one fact is how the
two drift apart.

It is called `visit`, not `transition`, for two reasons. `transition` is already
this wire's noun for an edge in a graph (`graph_transition`), and what is being
reported is a *completed visit* that happens to carry the transition that ended
it. The firmware calls it a `StateVisit` too, so the name is the one already in
the code.

| Field | |
|---|---|
| `trial_id` | The id from `configure`. **`0` when there was no host-configured trial** — demo mode, the bench, a line-started run before anything assigned an id. This is the whole of the "the MCU needs to optionally know the trial id" requirement, and it is already met: `configure` carries `trial_id` today and `TrialRecord` holds it |
| `seq` | Per run, from `0`. A gap is what makes a dropped visit **detectable** rather than a hole nobody notices |

#### Emitted at exit, and what that costs

The natural hook is `StateMachine::record_visit()`, which is called from
`leave()` with every field already computed. That means a visit is reported when
the state is **left**, not when it is entered — a visit's duration and exit cause
do not exist before then.

For the trace's purpose this is not a latency problem, because what matters is
the *timestamp*, not the arrival time, and `entered_us` is exact. For a live UI
it costs one state of lag on the very first state of a trial only: after that,
each exit tells the daemon both when the state it is reporting ended and — since
the daemon holds the graph and can resolve `transition_index` to its target —
which state the machine is in now. The one entry with no preceding exit is the
run's first, and it arrives when that state is left.

Cost per trial is one queued outbound message per visited state — call it six on
a go/no-go trial. Outbound messages go through `ReplyQueue` and are drained
without blocking a scan, and the measured 2.5 ms per *command* on USB CDC was
inbound-dominated: draining TinyUSB and reading, not writing. `tx_stalls` in
`state_report` already counts the case where the queue could not keep up.

**No switch in v1.** `event` is off by default because a word-on-change stream
can fire every scan; a visit stream fires a handful of times per trial, and there
is no rig configuration in which one would rather not have the record. A switch
gets added if, and only if, `tx_stalls` on a real board says the queue suffers —
and it would then live with the wiring config of §3.4, persisted, so a board
never comes back from a power cut quietly not tracing.

#### The stream is a preview; the result is the record

A dropped visit must not become a missing row. So the daemon **reconciles**: it
writes the trace live, and at `result_end` compares what it logged against the
authoritative path. Missing entries are filled from the result; a `seq` gap the
result does not fill is logged as an **error**, not passed over.

The residual case is a result that arrives `truncated`, and there the trace is
the only complete copy — the stream has no `kMaxPath`. That is a safety net, not
a reason to build any of this: the buffer should be sized so it never fires.

#### Size the buffer instead

The device's path buffer is not a design decision waiting to be made. It exists,
it is `StateVisit path[kMaxPath]` with a `path_truncated` flag, and the right
answer to a graph that loops is to make it big enough rather than to build
machinery around its overflowing.

| | |
|---|---|
| `StateVisit` | 16 B — three `uint8_t`, a pad, `drawn_ms`, `entered_us`, `duration_us` |
| `kMaxPath` today | 64, so 1 KB. `trial.h` already calls it *"about 1 KB on the reference board"* |
| The ceiling | **255.** `path_len` is a `uint8_t`, so 255 is the largest value the comparison in `record_visit` still terminates on, and 256 is the value that breaks it silently |
| At 255 | 4080 B, so **+3 KB** of the RA4M1's 32 |

255 is the number to take. It is not a compromise between two costs; it is the
point where the type stops the count, and going past it means widening
`path_len`, `result_begin`'s `path_len`, and the `from` index of every
`result_path` chunk — a protocol change for headroom nobody has asked for.

**Whether 255 is "enough" is computable, and the daemon should compute it.** It
holds the graph and it holds `cap_ms`, so the worst-case visit count of a trial
is `cap_ms` divided by the shortest path around each cycle — a check that belongs
next to the `caps` check `POST /api/session/graphs` already does, and one that
turns "big enough" from a hope into a validated property with a number attached.
It is only a real bound when every cycle in the graph has a positive minimum
duration; a cycle a graph can go round on transitions alone is bounded by the
scan rate, not by the timings, and for those the flag has to stay. Refusing them
is not the daemon's call — warning, with the count, is.

**And make it an actual ring**, which `config.h:34` already claims it is:
`record_visit` today keeps the **first** 255 visits and drops the rest, and for a
trial whose interesting part is the response at the end that is the wrong half.
Overflow should cost the oldest visits, not the newest. It is a head index and a
count instead of a bare `path_len`, plus two fields on the wire so the host is
never guessing which window it received:

| | |
|---|---|
| `result_begin.first_seq` | `u32` — the `seq` of the oldest visit still in the window |
| `result_begin.total_visits` | `u32` — how many the run actually made. `total_visits > path_len` is what `truncated` means, and now it says by how much |

The chunker walks the ring in order, so `result_path.from` keeps meaning an
offset into what was sent. This also makes the two layers the same shape — a ring
on the device, a ring in the daemon (§4.6), one overflow rule to explain instead
of two.

**One thing about that 3 KB.** It competes directly with §3.2's graph set, which
needs about 7.5 KB and is no longer contingent on anything: together they are
11.5 KB of 32, so the budget is tight rather than comfortable and open question
10 says not to spend the two separately.

#### The clock, and the wrap

Device microseconds wrap every ~71 minutes, so a raw `entered_us` is meaningless
in isolation — §4.5's whole reason for existing. The daemon sees every visit in
order and so can **unwrap** into a monotonic device timeline; `seq` is what makes
that trustworthy, because the one case it cannot resolve alone is a dropped visit
that straddles a wrap, and reconciliation closes that. Every trace line carries
the raw value *and* the host-clock estimate *and* the estimate's uncertainty. The
raw value is the evidence; the correlation is an estimate and is labelled as one.

#### What this changes below the daemon

Small, and it belongs in the same firmware branch as §3.3's work — both touch
`host_link_session.cpp` and both add to `PROTOCOL.md`, and reviewing them
together is cheaper than sequencing them:

| | |
|---|---|
| `machine/state_machine.cpp` | `record_visit()` gains an emit callback. The machine must not learn about the link, so it is a sink passed in, and `native` tests pass a vector |
| `protocol/host_link_session.cpp` | serialise `visit`, queue it, never block on it |
| `trial/trial_runner.cpp` | supplies `trial_id` to the sink; `0` when there is no trial |
| `core/config.h` | `STATEMACHINED_MAX_PATH` 64 → 255, and `STATEMACHINED_SAFE_LEVELS` (§3.4) |
| `machine/state_machine.cpp` | `record_visit()` becomes a real ring: overflow costs the oldest visit, not the newest, which is what `config.h:34` has always claimed |
| `protocol/host_link_session.cpp` | `result_begin` gains `first_seq` and `total_visits`; the chunker walks the ring in order |
| `dev/PROTOCOL.md` | §4 gains `visit`, and §4.3 gains the sentence that the result is authoritative and the stream is a preview |

---

## 4. The API

> **[`API.md`](API.md) is this section, written out.** It was written first, the
> way `PROTOCOL.md` was, and it is the document to change when the surface
> changes — what follows here is the *argument* for the shape, and it stays
> because the reasons are the part that is expensive to rediscover.

FastAPI + pydantic. Every model is a transcription of something already
specified — `PROTOCOL.md` for the device surface, triald's `dev/API.md` for the
outbound `OutcomeReport`. Documented in `dev/API.md` here, generated schema at
`/openapi.json`, and the web UI uses **only** this API — no private route,
which is what keeps the UI an honest test of it.

### 4.1 Device and lines

| | |
|---|---|
| `GET /api/device` | connected, board, `fw`, `proto`, measured `scan_hz`, `caps`, `has_set`, `set_version`, `n_graphs`, link counters, uptime |
| `GET /api/device/lines` | per line: index, direction, pin label, **your name for it**, invert, enable, safe level, debounce, and its live level |
| `PATCH /api/device/lines` | rename a line; change invert/enable/safe/debounce. Pushes the wiring config and persists it — §3.4 |
| `GET /api/device/firmware` | the running version against what the installed package ships. See §6.3 |

**Renaming a line is free** — names are the daemon's alone and never reach the
wire. The rest is the *wiring*, which after §3.3 is a standalone command rather
than four fields of `graph_begin`, so changing a debounce no longer reads as
"re-upload the graph". It is written to data flash in the same call, because the
whole point is that the board still has it after a power cycle with nobody
connected.

**What the daemon holds is a copy, not the truth.** `hello_ack` reports what the
board restored from flash, and the daemon reconciles: agreement is the normal
case, and a disagreement is surfaced rather than silently overwritten. A board
that has been moved between rigs is exactly the case where the config on the
bench and the config in the box differ, and quietly picking one is how a valve
ends up inverted.

### 4.2 Graphs

| | |
|---|---|
| `GET /api/graphs`, `GET·PUT·DELETE /api/graphs/{name}` | the store |
| `POST /api/graphs/{name}/validate` | every rule, plus this device's `caps`. Changes nothing |
| `POST /api/graphs/{name}/upload` | compile and upload one graph, as a set of one. → `set_version`. The bench and UI path — trying a graph out replaces whatever set is committed, so it is refused while a session is open |
| `POST /api/session/graphs` | the names a session will use. Compiled, checked against `caps` as a **set**, uploaded and committed. → `set_version`. See §4.3 |

### 4.3 The trial loop

triald drives; statemachined reports.

| | |
|---|---|
| `POST /api/trial/configure` | `{trial_id, graph, cap_ms, start, patch}` → armed. **`graph` is a name**; the daemon resolves it to a slot in the committed set and arms. Nothing is uploaded — see §3.2 |
| `POST /api/trial/start` | when `start` admits serial |
| `POST /api/trial/cancel` | `{trial_id, reason:"host"}` |
| `GET /api/state` · `WS /api/stream` | snapshot, and coalesced frames — triald's convention, and for the same reason: a slow browser tab must not hold up a session |

**`configure` never uploads.** Under §3.2 every graph the session uses is already
on the device, so the daemon maps the name to its slot and arms — one field on
one message. A `graph` naming something the store does not hold is **refused**
rather than guessed at, and one outside the committed set is refused too: a
session that declared its graphs and then asks for a fourth has a misconfigured
trial type, and the refusal says which name and which set. The reply still
carries the elapsed milliseconds, so an arm that took longer than it should is a
number per trial rather than an inference.

The exception is `graph_mode = "per_trial"` (§3.2, §4.4), where `configure`
uploads a set of one when the named graph is not the committed one and the reply
carries `uploaded: bool` and what it cost. That is a mode a rig opts into, and
triald sees the same call either way.

**Name → slot stays here.** triald names the graph it wants; the daemon knows it
is slot 3 because the daemon built the set. An index on triald's side would be
`TrialType.time_sequence` again — the field §3.1 says a name is the cure for —
and a cache of "which slot is this now?" to get wrong across a re-upload.

**`POST /api/session/graphs` is where a session is allowed to fail, and that is
the point.** triald declares the names a session will use — the union of what its
loaded sets' trial types reference, which only triald knows — and the daemon
compiles every one, checks the **summed** pool usage against the device's `caps`,
uploads the set and commits it, **before an animal is in the booth**. The
alternative is discovering at trial 40 that one trial type names a graph with 40
states on a 32-state board, and losing the session to it. It is the slowest call
in the API by a wide margin (§3.2: tens of seconds on a UART rig) and the one the
UI should show a progress bar for.

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
policy, the line map, whether to arm automatically on connect, `graph_mode`
(§3.2) and `trace_ring` (§4.6). Backed by `/etc/braemons/statemachined.toml`.

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

### 4.6 The trace

`GET /api/trace` · `GET /api/trace/trial/{trial_id}` · `WS /api/trace/stream`

`device/trace.py` decodes `visit`, unwraps the device clock, resolves indices to
**names** against the graph the daemon holds, and puts the result in a
**ring buffer in memory**. That ring is what every route above reads. Nothing in
the API touches a file.

**The ring, sized once.** A `collections.deque` with a `maxlen`, default
**100 000 entries**. A normal session is a thousand trials of half a dozen visits
— six thousand, so the default covers one fifteen times over; the pathological
case is a looping paradigm at the device's 255-visit ceiling for a thousand
trials, which is 255 000 and is what the `trace_ring` knob in §4.4 is for. At
roughly 200 B per slotted entry the default costs about 20 MB, which on a Pi 5
alongside vstimd is not a number worth optimising.

Keeping the served copy in memory is what makes the rest of this section small.
There is no "which file holds trial 193", no seek, no per-day file to select, no
index. `?trial_id=` and `?since_seq=` are a scan of a deque, and the Trace view's
tail is the deque's right end.

**Written as it arrives, not as it evicts.** The ring is volatile and a
`systemctl restart` during a package upgrade must not silently cost the morning's
traces, so each entry is also appended to `/var/lib/statemachined/trace/` as
NDJSON, one file per day, rotated by the logrotate config §6.2 already installs.
The daemon **never reads it back**: it is the copy for the analysis that happens
months later, not a second store with its own query path. Appending on arrival
rather than on eviction is deliberate — a crash otherwise loses exactly the
window that mattered most, which is everything still in the ring.

```jsonc
{"entry_number":4172,"kind":"visit","device_sequence_number":2,
 "trial_id":193,"graph":"go-nogo","set_version":7,
 "state_name":"Foreperiod","exit_cause":"transition",
 "fired_transition_target_state_name":"Cue","drawn_duration_ms":500,
 "entered_device_microseconds":500120,"unwrapped_device_microseconds":4795500120,
 "entered_host_time":"2026-09-03T14:22:07.481932Z",
 "host_time_uncertainty_microseconds":180}
```

**`entry_number` is the daemon's and is what `since_` means**, which this
sketch got wrong: the device's `seq` counts visits within a *run* and restarts
at zero every trial, so it cannot address a position in a log that spans a
session. Both are carried — `device_sequence_number` is what a gap is detected
with, `entry_number` is what a cursor is.

NDJSON rather than SQLite, for the same reason the wire is NDJSON: it is
append-only, so a crash mid-write costs the last line and not the file; it is
greppable on the rig at 2 a.m. with no tooling; and it copies. A database would
buy indexed queries nothing needs — the ring answers every query the API has, and
the file is never queried at all.

**`kind` is there because the device is not the only thing worth timestamping.**
The daemon puts its own events in the same ring, and the same file: `configure`, `start`, `cancel`
and their host times, link loss and reconnection, a graph upload and what it
cost, a `seq` gap that reconciliation could not fill. Aligning an external signal
to trial 193 needs to know when trial 193 was armed, not only which states it
visited, and splitting that across two streams means joining them later.

**This is not the `.tdr` and must not grow into one.** triald writes the trial
record; this is a finer-grained trace, one line per state visit rather than one
per trial, and it **joins to the `.tdr` on `trial_id`** — which is the entire
reason `trial_id` is on the wire. Nothing in it is a verdict; §1's "not a second
decision authority" applies here more than anywhere, because a log is exactly
where an opinion gets smuggled in unnoticed.

**triald may pull it, or not.** Pull, not push — triald's own convention, and the
one place in this plan where it is uncontested (unlike §4.3). If triald wants the
trace stowed beside the session's `.tdr`, it fetches `/api/trace/trial/{id}`
after the outcome; if it does not, the trace still exists on the rig. The daemon
never pushes it and never assumes anybody read it.

`WS /api/trace/stream` is the one stream that is **not coalesced**, unlike
`/api/state`'s: coalescing a state snapshot loses nothing, coalescing a trace
loses records. A client that cannot keep up gets its socket closed with the last
`seq` it received and re-fetches from `?since_seq=` — served from the ring, and
therefore only while the entry is still in it. A consumer slow enough to fall out
of a 100 000-entry ring has genuinely lost data and must be **told so**, with the
`seq` range that is gone, rather than handed a shorter answer that looks
complete. That is the one place the ring's boundedness is visible from outside,
and it is better than the alternative: buffering per client is how a monitoring
aid becomes the thing that fills the Pi's memory.

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
| **Trace** | the live tail of §4.6, one row per state visit, filterable by `trial_id`. The one view that is useful with nobody in the room, because it is still there in the morning |
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
- **Dependencies are small**: fastapi, uvicorn, pydantic, pyserial, httpx —
  what M4f actually installed, and no pydantic-settings: the config is one TOML
  file read with the standard library's `tomllib`, and a settings framework for
  one file would be a dependency bought to save four lines. Nothing like
  triald's numpy/scipy problem.
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
M4a–M4g; its M5–M7 shift down and need renumbering in that document.

| | |
|---|---|
| **M4a** ✅ | **The move, and nothing else.** `tools/bringup/` → `daemon/`, `statemachined_bringup` → `statemachined` with the wire under `device/`, `wire.py` made standalone with golden vectors against the emulator's copy *and* against `crc16.cpp`. The graph upload and result reassembly left `tests/hardware/hardware_test_harness.py` for `device/graph_set_upload.py` and `device/trial_result_reassembly.py`, so the hardware suite now tests the daemon's codec rather than a copy of it. `make bringup` and `make test-hardware` unchanged in behaviour; `make test-daemon` is new and is in `make ci` and CI |
| **M4b** ✅ | **Wiring config and the `visit` stream, in the firmware** (§3.3, §3.6). The wiring move plus a compile-time safe-level word closes a fail-safe hole that exists today on any rig whose outputs are not active-high, so it is worth doing whether or not the daemon ever ships; the stream is a callback and a serialiser. They share a file and a protocol document, so they share a branch. **No data flash** — that is deferred to M7 |
| **M4c** ✅ | **The graph set, in the firmware** (§3.2, §3.3): shared pools, `GraphEntry`, `set_begin`/`set_end`, a slot in `configure`, `max_graphs` in `caps`. The larger of the two firmware milestones and the one this plan's trial loop rests on. Covered by the native core, the Renode session and `PROTOCOL.md` message by message, all of which exist |
| **M4d** ✅ | `model/` and `graph_set_compiler.py`: the pydantic graph, the line map, names → wire. Host tests against `PROTOCOL.md` §3.2 message by message. `graphs/` gets go/no-go and 2AFC, which fills the directory `PLAN.md` has had empty since M0 |
| **M4e** ✅ | `device/device_supervisor.py` and `device_clock_correlation.py`: owns the port, reconnects, holds the seed, arms the watchdog, reassembles results. Integration-tested against the native core — whole trials, cancel races, link loss, as `PLAN.md` §Testing asks. It needed a host-side entry point for the firmware, which is now `firmware/native/statemachined_native_device.cpp`, and a `socket://` transport rather than the pty this row used to say — see §7's note |
| **M4f** ✅ | FastAPI: device, lines, graphs, trial, config, state/stream, and the trace of §4.6; the triald client; `statemachined serve`. [`dev/API.md`](API.md) written first, the way `PROTOCOL.md` was. It found `patch`: documented on the wire since M2 and implemented nowhere, so a host that sent one got a silently unpatched trial — see below |
| **M4g** | The web UI and the `/elements/` contract; mDNS |
| **M5** | Packaging: nfpm, systemd, sysusers, udev, logrotate, the builder containers, `release.yml`, one line in `packages/sources.txt`. **Installed on the Pi 5 alongside vstimd and triald** |
| **M6** | A whole session on the R4 with `triald sim`'s simulated subject replaced by the real board — which is what `PLAN.md`'s M4 actually asked for, and it needs everything above |
| **M7** | **Data flash** (§3.4): the `hal.h` addition, the RA4M1 implementation, a file-backed `native.cpp` stand-in, the boot-time read, and `persist`. Deferred deliberately: the compile-time safe levels of §3.4 hold the fail-safe hole shut without it, and this is easier to build once a daemon exists to exercise it. Renode covers the HAL addition |

**What M4b and M4c cost, measured.** The 255-entry path is +3056 B, exactly as
budgeted, and it briefly did not fit: demo mode carries a *second* `TrialRunner`,
so the bench image paid for the path twice and would not link. M4c paid that
back. A set is **single-buffered** — two do not fit — so the staged `StateGraph`
a single graph needed is gone, worth about 2.5 KB, and both images fit 255
again. `caps.max_path` is 255 everywhere, which is the state worth being in.

| | RAM, of 32 768 |
|---|---|
| rig image, before M4b | 12 928 B |
| rig image, now | **13 520 B** |
| bench image, now | 21 224 B |

So the set cost about 600 B net on a rig, not the ~7.5 KB §3.2 estimated —
because §3.2 was costing *bigger pools*, and M4c deliberately did not grow them.
The mechanism is in with today's capacities: twenty slots sharing 32 states, 64
transitions, 64 actions and 32 distributions, which is four graphs of eight
states. **Growing the pools is the follow-up**, and it now has ~9 KB of headroom
and a real `.map` to argue from rather than an estimate — see open question 10.

`platformio.ini` also said `MAX_PATH=1024` for the Teensy and `512` for the
ESP32, neither of which was ever possible: `path_len` is a `uint8_t`. A
`static_assert` in `config.h` refuses it now instead of leaving it to be found
on a board.

**The integration tests talk to the firmware, not to a mock**, and that needed
one new thing: `firmware/native/statemachined_native_device.cpp`, the same
`HostLinkSession` and the same engine driven by an ordinary loop instead of by a
timer ISR. What it buys is the class of bug a mock cannot have — a mock answers
what the test author believed the protocol says, and this answers what
`firmware/core/protocol` says, refuses what it refuses, and reassembles a result
out of the same chunker.

**Not over a pty, in the end.** The plan said pty and pty turns out to be the
awkward choice: the native device takes its link on stdin and stdout, and
pyserial opens a *path*, so the two ends of a pty pair cannot both be reached
that way — the daemon side would have to bypass `SerialLink` and use the master
file descriptor raw, which is exactly the code path a test should not skip. So
the harness bridges a TCP socket to the child's pipes and the daemon connects
with `socket://127.0.0.1:<port>`: a URL a rig genuinely uses, and one that keeps
the transport under test the transport the daemon ships.

**What M4f found.** `configure`'s `patch` — per-trial distribution overrides —
has been in `PROTOCOL.md` §3.3 since M2 and was implemented in no layer at all.
A host sending one got a trial whose timings were quietly the unpatched ones,
which is the class of failure this firmware is least willing to have. It is
implemented now, in the firmware, with the *inverse* of the patch stored so a
trial's overrides come off when it ends; a malformed patch applies nothing, and
a patch that outlived its trial would be a timing nobody could account for.

**Two things about the daemon's shape are worth writing down**, because neither
is in §4 and both would otherwise look arbitrary in the code.

*One lock, held by everything that talks to the device.* A serial link is not
reentrant, and the alternative — a queue with a reader matching replies to
requests — is the right shape for a link serving many callers concurrently. This
one serves triald in a strict request/response loop plus a browser tab. Coarse
locking is honest about that; a queue would be machinery for a concurrency this
rig does not have.

*A thread that reads the link when nobody asked it to.* A result and the `visit`
stream arrive unasked. Without that thread they would sit in the kernel's buffer
until the next command happened to read them, and a trace whose timestamps are
the device's but whose *arrival* is whenever somebody next asked is not a trace.
It reads in 50 ms bursts under the lock, so a request never waits long for it,
and it carries the heartbeat `ping` — which the link-loss watchdog wants anyway,
and which is the clock correlation's observation for free.

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
4. **What a whole-set upload costs at session start** (§3.2), for USB CDC and
   for the SCI2 UART. It is no longer the question the design turns on — §3.2
   switches by index, so the per-trial cost is a field — but it bounds how long
   `POST /api/session/graphs` blocks, and the estimate for it is extrapolated
   from a per-command measurement rather than measured over a burst. Answerable
   the day M4e can upload against hardware; belongs in `HARDWARE.md` next to the
   scan rate. Two ways it could come back badly: the per-command cost is worse
   under a long burst than the single-command figure suggests, or a rig wants to
   change a graph mid-session often enough that "between sessions" is the wrong
   granularity.
5. **`TrialType.graph` is triald's field to add**, alongside or replacing
   `time_sequence`. Same amendment as #3, and probably the same patch.
6. **Global timers** (`PLAN.md` open question 3, still open) are the one feature
   likely to change `model/graph_definition.py`'s shape. Not in v1, but the model should be
   written so they are an addition rather than a rewrite.
7. **Who authors the console**, and when. §5 defers it; it should not stay
   deferred long, since it is most of what makes three daemons feel like one rig.
8. **The RA4M1 data-flash numbers** (§3.4): 8 KB and ~100 000 erase cycles are
   from memory, not from the datasheet. Cheap to confirm, and #12 depends on the
   second one being roughly right. **Deferred with M7** — but confirm it before
   M7 is planned in detail rather than during it, since a figure an order of
   magnitude out changes whether `persist` is worth having at all.
9. **Trace retention.** Mostly answered by the ring: nothing in the daemon
   depends on what logrotate deletes, because the API reads the ring and never
   the file. What is left is how long the file is kept and by what rule, which is
   a line in the logrotate config and a decision about the rig's disk rather than
   a design question. The version worth arguing about is whether it is kept at
   all — if the answer to #11 is that triald stows every trial's trace beside the
   `.tdr`, the daemon's file is a duplicate and could go.
10. **How big the shared pools should be.** Answered halfway. M4b and M4c are
   both in and the rig image is 13 520 B of 32 768, so the fear that they would
   not fit together was wrong — single-buffering the set paid for most of the
   path. What is *not* done is growing the pools: the set holds twenty slots
   sharing today's 32 states and 64 transitions, which is four graphs of eight
   states, and §3.2's arithmetic wants ~160 and ~240. There is room for it now
   (about 9 KB once the framework's 8 KB heap and the stack are taken), and it
   is a one-line change per capacity in `platformio.ini` plus a `.map` to check
   — deliberately not done in the same commit as the mechanism, so that a
   sizing decision is argued from a measurement rather than mixed into a
   refactor. Note `TransitionState` is 12 B per transition *per machine*, and a
   bench build has two.
11. **Does triald stow the trace beside the `.tdr`?** §4.6 leaves it optional and
   that is right for the daemon, but "optional" across two daemons usually means
   "nobody did it". If the answer is yes it is a triald change, and it lands with
   the amendments in #3 and #5.
12. **Who is allowed to call `persist`, and how often** — deferred with M7, and
   the reason the rule is written down now rather than then. "Explicit, never on
   `graph_end`" is the rule that protects the part, and a rule enforced only by
   the daemon's good manners is one a future caller breaks. Worth a write counter
   in `state_report`, so the budget is observable rather than trusted — the same
   argument `overruns` won.
