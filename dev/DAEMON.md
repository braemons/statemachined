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

### 3.2 Switching graphs: wait for the trial to end, and measure before optimising

**The graph changes between trials, so the upload happens between trials.** The
daemon uploads on `configure`, `armed` comes back when it is done, and triald —
which is pull-based and asks for a trial when it is ready — simply gets its
answer a little later. No firmware change, and the device's existing
double-buffering does exactly what `PLAN.md` built it for: a failed upload leaves
the running paradigm intact.

This is the design until a board says otherwise. What follows is why that is very
likely enough, and what to do if it is not.

#### What an upload actually costs

Not what an earlier draft of this section claimed. It reasoned from 115 200 baud
and got about a second, which is wrong on the reference board: the link is USB
CDC, where the baud rate is nominal and the transport is 12 Mbit/s full-speed
USB. Throughput was never the constraint.

The real cost is **per command**, and commit `79d50e4` measured it on hardware
while chasing a climbing overrun count:

| | |
|---|---|
| draining the link (TinyUSB) | 1240 µs |
| reading the link (TinyUSB) | 754 µs |
| `tud_task()`, via `link_up()` | 533 µs |
| `HostLinkSession` — ours | 372 µs |
| the scan itself | 7 µs |

≈ **2.5 ms per command, almost all of it vendor USB stack.** A ten-state graph is
roughly fifty messages, so **≈ 125 ms device-side**, call it 200–400 ms once the
host's round-trips are counted. That fits inside any ITI worth having, and it is
the number the whole question turns on.

**It is different on a UART rig.** `uno_r4_minima_sci` puts the host on SCI2,
where 115 200 baud is real and ~15 kB of upload traffic *is* about 1.3 s. So the
answer is per transport, and `HARDWARE.md` should carry both.

#### The one thing to design against

An upload that happens only when the trial type *changes* makes the ITI
systematically longer on exactly those trials — a timing difference **correlated
with the variable under study**, which is a confound rather than an
inconvenience. Every ordering triald offers draws without replacement, so type
changes are frequent and irregular.

The fix is cheap and should be in from the start: **pad to a constant**. The
daemon reports what the switch cost, triald holds the ITI to a fixed floor above
it, and the animal sees no difference between a repeat and a switch. At 200 ms
that floor is not a real constraint.

#### The measurement that decides

`graph_switch_ms` on a real R4, for both transports, next to the scan rate in
`HARDWARE.md`. The harness for it already exists — `test_scan_health.py` measures
what a command costs the scan, and this is the same question asked of fifty of
them. It is answerable the day M4c can upload against a board.

Two ways it could come back badly: the per-command cost is worse under a long
burst than the single-command figure suggests, or a rig wants an ITI shorter than
the floor. Then, and only then:

#### If the measurement says no: the graph set

**Hold every graph the session uses and switch by index**, so no upload happens
between trials at all. Sketched here because the arithmetic is the interesting
part and it is better done before it is needed than under pressure.

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

So graphs would share one set of pools — **the mechanism the firmware already
uses one level down**, where a `State` addresses its transitions and actions as
`(first, count)` slices. A graph is the same thing one level up, at three bytes:

```c
struct GraphEntry { StateIndex entry, first_state; uint8_t n_states; };
```

Twenty of those is 60 B. `InputConfig` would stop being per-graph in the same
move: invert, enable and debounce describe **the wiring**, not the paradigm, so
one copy per device is both cheaper and more nearly true than what is there now.
That part is worth doing whether or not the rest is.

**The ceiling is `uint8_t`, not RAM.** `StateIndex`, `TransitionIndex`,
`OutputActionIndex` and `RandomDistributionIndex` all use `0xFF` as a sentinel,
so 255 is a hard limit per pool across the whole set, and widening one is a type
change reaching every struct and the wire format. Twenty graphs would budget ~160
states, ~240 transitions, ~200 actions — about **8 states and 12 transitions
each**, in ≈ 7.5 KB against ~11.6 KB unclaimed. Two sets would not fit without
reclaiming the framework's 8 KB heap, so the device would hold one, invalid until
committed: a failed upload leaves *no* graph rather than the previous one, which
fails safe and is visible where the alternative failure was silent.

Teensy 4.1 and ESP32 have none of these problems. The R4 sets the design, as
`PLAN.md` intends.

#### The variant not to reach for first

Uploading *during* a trial, into the staging buffer, and committing when the
trial ends — near-zero ITI cost. It is refused today
(`host_link_session.cpp:251`, `busy` / `"a trial is armed or running"` / context
`"graph upload"`) and relaxing that is a small change, since staging is already a
separate buffer.

But it trades ITI time for ~2.5 ms of USB stack, fifty times, **during the trial
being timed**. `79d50e4` moved the scan into the timer ISR so the link no longer
costs scan periods, which makes this thinkable where it was not before — and
"thinkable" is as far as it should go until `graph_switch_ms` and a fresh
`overruns` count under load say more. Jitter on the trial you are measuring is a
worse failure than a longer ITI, and it is harder to see.

### 3.3 What this changes below the daemon

**Nothing for graph switching.** The per-trial upload uses the protocol and the
firmware exactly as they stand, which is the strongest argument for doing it that
way first. M2 and M3 stay closed on that count.

**One change is worth taking regardless of any measurement**, and §3.4 turns it
from tidiness into a fail-safe fix: move `InputConfig` and `output_safe_levels`
out of `StateGraph` to device scope. They describe the wiring rather than the
paradigm; re-sending them with every graph is what makes "change the debounce"
read as "re-upload the graph" in §4.1, and keeping them only inside a graph is
why a board with no graph cannot fail safe correctly.

That, plus persistence, is one small firmware milestone — **M4b′** in §7:

| | |
|---|---|
| `graph/state_graph.h` | `InputConfig` and `output_safe_levels` move out to device scope |
| `core/hal.h` | an eighth function: read and write the data flash, on a boundary that already exists |
| `hal/renesas_ra4m1.cpp` | the RA4M1 data-flash implementation. `native.cpp` gets a file-backed stand-in so the host tests still cover the path |
| `src/main.cpp` | read the wiring config before the first `fail_safe()`, and the graph after it |
| `protocol/host_link_session.cpp` | a `wiring` command, a `persist` command, both refused with `busy` during a trial |
| `dev/PROTOCOL.md` | §3 gains those two; §4.1 `hello_ack` reports what was restored, so the daemon never guesses whether the board came up configured |

Should the graph set of §3.2 also be needed, it is `config.h`,
`graph/state_graph.h`,
`protocol/graph_builder.cpp`, `protocol/host_link_session.cpp`, `PROTOCOL.md`
§3.2/§3.3/§4.1/§4.2, and the Renode session — a milestone of its own, and not a
Python one. `proto` would stay **1** on the precedent `PROTOCOL.md` set at M3 for
the `seq` → `message_id` rename: no bridge has shipped, so there is no deployed
peer to keep compatible.

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

> **It must never be automatic on `graph_end`.** Under §3.2 the daemon uploads a
> graph *per trial*. Persisting each one would spend the part's ~100 000 erase
> cycles in about a hundred thousand-trial sessions — silently, and presenting as
> a board that one day stops accepting graphs. So it is a distinct command the
> daemon issues deliberately (at session setup, or from the UI), and `graph_end`
> never writes flash on its own.

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

This buys one thing back that the result cannot give. `result_begin` carries
`truncated` for a run that visited more states than `kMaxPath` holds — *"a graph
may loop, and a long trial degrades to a truncated path rather than to a corrupt
one"*. **The live stream does not truncate.** For exactly the runs where the
record is lossy, the trace is complete, which turns a looping graph from a
recording problem into a normal one.

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
| `dev/PROTOCOL.md` | §4 gains `visit`, and §4.3 gains the sentence that the result is authoritative and the stream is a preview |

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
| `POST /api/graphs/{name}/upload` | compile, upload, commit. → `graph_version`. Used by `configure`, by the UI, and to pre-warm before a session |
| `POST /api/session/graphs` | the names a session will use. Validated and *not* uploaded — see §4.3 |

### 4.3 The trial loop

triald drives; statemachined reports.

| | |
|---|---|
| `POST /api/trial/configure` | `{trial_id, graph, cap_ms, start, patch}` → armed. **`graph` is a name**; the daemon uploads it if it is not the committed one, then arms. See below |
| `POST /api/trial/start` | when `start` admits serial |
| `POST /api/trial/cancel` | `{trial_id, reason:"host"}` |
| `GET /api/state` · `WS /api/stream` | snapshot, and coalesced frames — triald's convention, and for the same reason: a slow browser tab must not hold up a session |

**`configure` uploads only when it must**, which under §3.2 is when the named
graph is not the committed one — between trials, on a device that is idle, at the
measured cost §3.2 budgets. A `graph` naming something the store does not hold is
**refused** rather than guessed at, and one outside the set `POST
/api/session/graphs` declared is refused too: a session that declared its graphs
and then asks for a fourth has a misconfigured trial type, and uploading it
anyway would hide that behind a trial that armed late.

**`configure` is one call, not two, and that is deliberate.** triald names the
graph it wants; the daemon compares it against the committed `graph_version`,
uploads only if they differ, and arms. Splitting it into `upload` then
`configure` would put "has this graph already been uploaded?" in triald —
bookkeeping about a device it does not own, and a cache to get wrong. The reply
carries `uploaded: bool` and the elapsed milliseconds, so §3.2's cost is a number
per trial rather than an inference from a trial that armed late.

**`POST /api/session/graphs` is the failure this prevents.** triald declares the
names a session will use — the union of what its loaded sets' trial types
reference, which only triald knows — and the daemon validates every one against
the device's `caps` **before an animal is in the booth**. It uploads nothing. The
alternative is discovering at trial 40 that one trial type names a graph with 40
states on a 32-state board, and losing the session to it.

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

### 4.6 The trace

`GET /api/trace` · `GET /api/trace/trial/{trial_id}` · `WS /api/trace/stream`

`device/trace.py` decodes `visit`, unwraps the device clock, resolves indices to
**names** against the graph the daemon holds, and appends one line per visit to
`/var/lib/statemachined/trace/` — NDJSON, one file per day, rotated by the
logrotate config §6.2 already installs.

```jsonc
{"kind":"visit","seq":2,"trial_id":193,"graph":"go-nogo","graph_version":7,
 "state":"Foreperiod","exit":"transition","transition":0,"to":"Cue",
 "drawn_ms":500,"entered_us":500120,"duration_us":183044,
 "entered_host":"2026-09-03T14:22:07.481932Z","clock_err_us":180}
```

NDJSON rather than SQLite, for the same reason the wire is NDJSON: it is
append-only, so a crash mid-write costs the last line and not the file; it is
greppable on the rig at 2 a.m. with no tooling; and it copies. A database would
buy indexed queries the daemon does not need — the API's queries are *by trial*
and *recent*, and a session's worth of trace is a megabyte.

**`kind` is there because the device is not the only thing worth timestamping.**
The daemon logs its own events into the same file: `configure`, `start`, `cancel`
and their host times, link loss and reconnection, a graph upload and what it
cost, a `seq` gap that reconciliation could not fill. Aligning an external signal
to trial 193 needs to know when trial 193 was armed, not only which states it
visited, and splitting that across two files means joining them later.

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
`seq` it received, and re-fetches from `?since_seq=`. Slow-consumer buffering is
how a monitoring aid becomes the thing that fills the Pi's memory.

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
| **M4b′** | **Wiring config, persistence, and the `visit` stream, in the firmware** (§3.3, §3.4, §3.6). The one firmware milestone here, and small. The first part closes a fail-safe hole that exists today on any rig whose outputs are not active-high, so it is worth doing whether or not the daemon ever ships; the second is a callback and a serialiser. They share a file and a protocol document, so they share a branch. Renode covers the HAL addition; `native.cpp` gets a file-backed stand-in so the host tests reach it too |
| **M4b** | `model/` and `compile.py`: the pydantic graph, the line map, names → wire. Host tests against `PROTOCOL.md` §3.2 message by message. `graphs/` gets go/no-go and 2AFC, which fills the directory `PLAN.md` has had empty since M0 |
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
4. **`graph_switch_ms` on a board** (§3.2), for USB CDC and for the SCI2 UART.
   Everything in this plan's handling of graph switching rests on it, and one
   estimate here has already been wrong by a factor of five in the safe
   direction. Answerable the day M4c can upload against hardware; belongs in
   `HARDWARE.md` next to the scan rate. **This is the question most likely to
   change the plan** — if it comes back badly, §3.2's graph set is the answer and
   it is a firmware milestone, not a Python one.
5. **`TrialType.graph` is triald's field to add**, alongside or replacing
   `time_sequence`. Same amendment as #3, and probably the same patch.
6. **Global timers** (`PLAN.md` open question 3, still open) are the one feature
   likely to change `model/graph.py`'s shape. Not in v1, but the model should be
   written so they are an addition rather than a rewrite.
7. **Who authors the console**, and when. §5 defers it; it should not stay
   deferred long, since it is most of what makes three daemons feel like one rig.
8. **The RA4M1 data-flash numbers** (§3.4): 8 KB and ~100 000 erase cycles are
   from memory, not from the datasheet. Cheap to confirm, and #11 depends on the
   second one being roughly right.
9. **Trace retention on a rig nobody visits.** A session is about a megabyte,
   so size is not the problem; rotating away a trace that no analysis has copied
   yet is. logrotate with a generous keep is the mechanism; the policy question
   is whether the daemon should refuse to rotate a file whose trials triald has
   not fetched, which would make it stateful about a consumer it otherwise knows
   nothing about. Leaning: no — rotate on time and size, and let §4.6's pull be
   triald's job to actually do.
10. **Does triald stow the trace beside the `.tdr`?** §4.6 leaves it optional and
   that is right for the daemon, but "optional" across two daemons usually means
   "nobody did it". If the answer is yes it is a triald change, and it lands with
   the amendments in #3 and #5.
11. **Who is allowed to call `persist`, and how often.** "Explicit, never on
   `graph_end`" is the rule that protects the part, and a rule enforced only by
   the daemon's good manners is one a future caller breaks. Worth a write counter
   in `state_report`, so the budget is observable rather than trusted — the same
   argument `overruns` won.
