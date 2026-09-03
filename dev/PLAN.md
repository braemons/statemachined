# fsmd — the plan

> **Status:** M0-M3 implemented. The portable core, the wire protocol and the
> Uno R4 Minima HAL are written, unit-tested on the host (10 suites, green under
> gcc/clang, ASan/UBSan and at `-O0`/`-O3`) and exercised on an emulated board
> under Renode. **No physical board has run this yet**, so the 10 kHz scan rate
> remains a claim; the RAM figure has been measured against a real link step and
> is recorded below. Next up is **M4, the bridge to triald** — `bridge/` and
> `graphs/` are still empty. Milestones and their state are at the end.

## What fsmd is

The **trial state machine** of a braemons rig, running on a microcontroller.
It is the participant that `triald/dev/PLAN.md` calls "the MCU": the half of
VStim's interval table that vstimd's armed animations cannot express — response
windows, timeouts, reward, and **naming the outcome**.

It steps through a finite set of states. Each state has a map of triggers to a
next state, a timeout, and a set of output actions. Some states are terminal and
name a trial outcome. Between trials it sits idle, waiting to be armed again.

```
   triald            configure / arm / result            slow bus · HTTP+JSON
  ┌────────┐  ◀────────────────────────────────▶  ┌──────────┐
  │ triald │                                       │ fsmd     │  host bridge
  └────────┘                                       │ (bridge) │
  ═══════════════════════════════════════════════  └────┬─────┘
                                                        │ USB CDC · NDJSON
  ┌─────────┬─────────┬─────────┬──────────┬────────────┴──────┐
  │ vstimd  │ soundd  │ optod   │  daqd    │  fsmd (firmware)  │
  │ armed   │ audio   │ laser   │ TTL ⇄ VTL│  states · outputs │
  │ anims   │         │         │          │  outcome · reward │
  └─────────┴─────────┴─────────┴──────────┴───────────────────┘
              coupled to each other by trigger edges
```

**The division of authority is triald's, unchanged.** fsmd is the *timing*
authority: it debounces inputs, timestamps in its own clock, drives the valve,
and names the outcome. triald is the *decision* authority: it chooses the trial
type, decides whether the outcome was *accepted*, and records what happened.

**The firmware never learns the trial type.** It receives a graph, timings and a
reward duration. That is what keeps firmware stable while paradigms change.

### The name

**`fsmd`** — the job, not the hardware. It follows the family's `<function>d`
convention (`vstimd`, `triald`, `soundd`, `optod`, `daqd`), and an acronym plus
`d` already has precedent in `daqd`.

Deliberately **not** `mcu-fsmd`. vstimd ships `gpiochip-daqd`, and that name is
`<backend>-<daemon>`: `daqd` is the daemon and owns the protocol, gpiochip is one
backend of it. Spending the generic slot on a backend would leave nothing to name
the thing the backends have in common — which is most of this document.

Rejected, with the reasons, so nobody re-proposes them: `stated` and `outcomed`
read as English past tenses in every sentence you would write about them;
`statd` collides with `rpc.statd` on every Linux box running NFS; `mcud` names
the peripheral rather than the job and would freeze into the ecosystem a hardware
fact this design exists to make irrelevant.

| | |
|---|---|
| `fsmd` | The project, the wire protocol, the graph format, the host bridge |
| `fsmd-firmware` | The portable core plus HALs; packaged per board — `fsmd-firmware-uno-r4`, `fsmd-firmware-teensy41` |
| `gpiochip-fsmd` | *If ever needed* — the Linux/Pi backend, named exactly as `gpiochip-daqd` is |

---

---

## Prior art and acknowledgements

### Bpod (Sanworks)

**[Bpod](https://sanworks.io/shop/products.php?productFamily=bpod)**
([github.com/sanworks](https://github.com/sanworks)), by Josh Sanders, out of the
Kepecs lab, is the closest existing system to this one and the direct
intellectual ancestor of the model below. Bpod is open-source hardware and
firmware sold commercially, Teensy-based, with MATLAB and Python clients.

The core idea we take from it is **the state matrix as the per-trial unit of
configuration**: the host uploads a table of states, each with a timer, a map of
events to next states, and output actions; the device runs it and reports back
the state path with timestamps. That shape is Bpod's, it is right, and we are not
pretending otherwise.

Where fsmd differs, and why:

| | Bpod | fsmd |
|---|---|---|
| Host loop | MATLAB/Python owns the trial loop directly | triald owns it; fsmd is one participant on a trigger bus alongside vstimd, soundd, optod, daqd |
| Outcome vocabulary | the state name is the result; the host interprets | terminal states name an `.tdr` outcome code from triald's fixed 11-code taxonomy, a wire contract with years of files behind it |
| Coupling to stimuli | Bpod modules and TTL | vstimd Virtual Trigger Lines, bridged to real TTL by daqd |
| Hardware | dedicated Bpod state machine boards | commodity boards — Uno R4 Minima first, then Teensy 4.1 and ESP32 — via a portable core |
| Randomised timings | host-drawn, pushed per trial | device-drawn from a host-seeded deterministic PRNG, with every realised value reported back |

If you already run Bpod, run Bpod. fsmd exists because the rest of the braemons
stack — the VTL trigger bus, triald's acceptance and outcome accounting, the
`.tdr` record — needs a participant shaped to *it*, on whatever board a lab
happens to have.

### What we borrow from Bpod's firmware

`Bpod_StateMachine_Firmware` (Sanworks LLC, **GPLv3**) is one ~3,000-line `.ino`
per board. Reading it settled several questions and reopened one. **If we lift
code, fsmd's firmware is GPLv3** — which is the proposal in *Open questions*
anyway, so there is no conflict to resolve.

**Where it independently confirms us**

- The scan is a **hardware-timer ISR** at `timerPeriod = 100` us — 10 kHz, the
  same number this plan reached separately. Take the mechanism too: a timer ISR,
  not a busy loop in `loop()`.
- *"The first event linked to a state transition takes priority"* — declaration
  order resolves ties, exactly as specified under *Triggers*.
- Graph upload is confirmed before use (`smaTransmissionConfirmed`) and a newly
  uploaded machine arms for the *next* trial (`RunStateMatrixASAP`), which is the
  shape of our `graph_end` + `graph_version` handshake.

**Where its representation is wrong for our smallest board**

Bpod's state matrix is **dense**: `InputStateMatrix[MaxStates+1][InputMatrixSize]`
indexed `[state][event_code]`, plus parallel matrices for global timer starts,
timer ends, counters and conditions. Lookup is O(1), which is elegant — and it is
why Bpod needs an Arduino Due or a Teensy. Their own note on the feature
profiles: *"Declaring more of these requires more MCU sRAM, often in ways that
are non-linear."*

Our per-state condition **lists** are sparse and small (see *Fitting on 32 KB*),
at the cost of scanning predicates rather than indexing an array. Two rules keep
that cheap, and they are requirements, not optimisations:

1. **Only the current state's conditions are evaluated.** Cost is bounded by
   conditions-per-state — typically one to five — never by graph size.
2. **Skip evaluation when the input word is unchanged** since the last scan.
   Compare one `uint32_t`; the overwhelmingly common case at 10 kHz costs a
   compare and a branch. Timers still tick.

**A wart to avoid.** Bpod detects a transition with `NewState != CurrentState`,
so a **self-transition is silently a no-op**. fsmd supports explicit
self-transitions: re-entering a state resets its timer and **redraws its random
duration**, which makes a re-triggerable timeout expressible.

**Its condition model is narrower than ours, which is the whole reason for the
mask design.** A Bpod `Condition` is `ConditionChannels[i]` plus
`ConditionValues[i]`: *one* channel, *one* value, a level check. Combinations of
TTL lines are not expressible in Bpod either. The three-mask predicate subsumes
Bpod's Conditions entirely and adds combinations at no extra runtime cost.

**Four features to take outright**

| Bpod | What it is, and why we want it |
|---|---|
| `logicHigh[]` / `logicLow[]` | Per-channel **inverted input logic**. Opto-isolators invert. This plan had output safe levels and no input polarity — a real omission. Add `invert` and `enabled` per input line |
| `inputOverrideState` / `outputOverrideState` | Host-injected **virtual events**, and output overrides that lock a line against state changes until released. The same idea as raising a vstimd VTL by hand, and the basis of a manual-control panel |
| `SyncChannel` + `SyncMode` | A dedicated sync line: mode 0 pulses on trial start/end, 1 toggles on every state change, 2 free-runs at 10 Hz. This **answers open question 5** on alignment to the ephys clock |
| Per-state output *and* virtual actions | Their output matrix includes global-timer trigger/cancel and counter-reset as pseudo-outputs, keeping one uniform action vocabulary. Worth copying if we adopt global timers |

**Where we knowingly diverge: the wire.** Bpod speaks compact **binary** op-codes
over `ArCOM`. It is cheaper than JSON and they are right that it is. We pay the
parsing cost on purpose — triald's PLAN.md rejects *"a protocol nobody can
`curl`"* — and the chunked upload in *Fitting on 32 KB* is what makes that
affordable on 32 KB. Bpod also has no per-trial identifier on its messages; our
`trial_id` discipline comes from triald and stays.

### Also worth naming

- **VStim** (Andreas Kreiter, Cognitive Neurophysiology Lab, Bremen) — `ExpCtrl` /
  `Interval` / `TimeSqz` / `Valve`, ~4,700 lines, is the machine this decomposes.
  See triald `dev/PLAN.md`, *The interval table, decomposed*.
- **pyControl** (Thomas Akam) — Python-on-micropython behavioural hardware, a
  different and also excellent answer to the same question.
- **Autopilot** (Jonny Saunders) — distributed rig control on Raspberry Pi.
- **MonkeyLogic** — the state-machine-over-MATLAB lineage a lot of primate rigs
  still run.

---

## The model

### A graph

A graph is a set of **states**, one of which is the entry state, plus a set of
**terminal** states each naming an outcome. It is uploaded as JSON, validated on
arrival, and held across trials.

```jsonc
{
  "graph_version": 7,
  "inputs":  ["lever", "lick_left", "lick_right", "stim_onset", "abort"],
  "outputs": ["valve", "house_light", "cue_led", "sync_out"],
  "entry": "wait_start",
  "states": {
    "wait_start": {
      "on_entry":  [{"line": "house_light", "action": "high"}],
      "timeout":   {"dist": "fixed", "ms": 5000, "goto": "not_started"},
      "on": [
        {"when": {"all": ["lever"]}, "goto": "foreperiod"}
      ]
    },
    "foreperiod": {
      "on_entry": [{"line": "sync_out", "action": "pulse", "ms": 5}],
      "timeout":  {"dist": "exponential", "min_ms": 500, "max_ms": 3000,
                   "mean_ms": 1200, "goto": "response_window"},
      "on": [
        {"when": {"none": ["lever"]}, "goto": "early"}
      ]
    },
    "response_window": {
      "timeout": {"dist": "fixed", "ms": 1000, "goto": "late"},
      "on": [
        {"when": {"all": ["lick_left"], "none": ["lick_right"]}, "goto": "reward"},
        {"when": {"any": ["lick_right"]},                        "goto": "wrong"}
      ]
    },
    "reward": {
      "on_entry": [{"line": "valve", "action": "high"}],
      "on_exit":  [{"line": "valve", "action": "low"}],
      "timeout":  {"dist": "fixed", "ms": "$reward_ms", "goto": "hit"}
    },

    "hit":         {"outcome": "HIT"},
    "wrong":       {"outcome": "WRONG_RESPONSE"},
    "early":       {"outcome": "EARLY"},
    "late":        {"outcome": "LATE"},
    "not_started": {"outcome": "NOT_STARTED"}
  }
}
```

Terminal states carry `outcome` and nothing else. `outcome` is one of triald's
eleven `.tdr` codes, by name or by number — **never renumbered**:

```
-1 UNDETERMINED   2 WRONG_RESPONSE        5 EARLY      8 INEXPECTED_START_SIGNAL
 0 NOT_STARTED    3 EARLY_HIT             6 LATE       9 WRONG_START_SIGNAL
 1 HIT            4 EARLY_WRONG_RESPONSE  7 EYE_ERROR 10 CANCELLED
```

`"$reward_ms"` is a reference into the per-trial patch (below). Substitution is
by name and resolved at arm time, not at parse time.

### Triggers, including combinations of TTL lines

**This is the part that generalises furthest, so it is worth getting exactly
right.** A trigger is not "an edge on line 3". It is a **predicate over the whole
input word**, and it fires on the predicate's own rising edge.

Every input line is sampled into a `uint32_t` word each scan. A condition is
three bitmasks:

```jsonc
{"all": ["lever", "lick_left"], "any": ["cue_a", "cue_b"], "none": ["abort"]}
```

evaluated as, in constant time and with no allocation:

```
(w & all_mask) == all_mask  &&  (any_mask == 0 || (w & any_mask) != 0)
                            &&  (w & none_mask) == 0
```

The predicate is evaluated every scan; the transition fires on the **false→true
edge of the predicate**, not on its level. That single rule subsumes everything:

- a plain rising edge on one line is `{"all": ["lever"]}`
- a falling edge is `{"none": ["lever"]}`
- "both levers down at once" is `{"all": ["lever_l", "lever_r"]}` — and it fires
  on the transition into that combination however the animal gets there, which
  is the behaviour you want and is fiddly to write by hand with per-line edges
- "any lick port" is `{"any": [...]}`
- "responded while not holding" is `all` plus `none` in one condition

Two qualifiers, both cheap and both genuinely needed:

| Field | Meaning |
|---|---|
| `hold_ms` | the predicate must stay continuously true this long before the transition fires. "Both levers held for 200 ms." Restarts if it drops. |
| `level: true` | fire even if the predicate is *already* true on state entry. Default is false — edge semantics — so "wait for a press" does not fire when the lever is already down. `level` is "wait until held", the other is "wait for the press". |

`hold_ms` accepts the same distribution vocabulary as `timeout`, so a required
hold can be randomised too.

**Resolution order is declaration order.** Two conditions in a state can become
true on the same scan; the first in the list wins. This is wire-visible
behaviour, so it is specified rather than incidental, and graph validation warns
on a condition that is unreachable because an earlier one subsumes it.

**Per-line input configuration**, set once with the graph and applied before the
word is assembled:

| | |
|---|---|
| `debounce_ms` | Default 1 ms. Per line rather than per condition, which is what stops combination predicates chattering |
| `invert` | Inverted logic, from Bpod's `logicHigh`/`logicLow`. Opto-isolated inputs are routinely active-low, and pushing this into the word assembly means no graph ever has to know |
| `enabled` | A disabled line reads as 0 and raises nothing |

Doing all three at word-assembly time means every predicate above sees clean,
polarity-normalised bits, and none of the condition logic has a special case.

### Outputs

`on_entry` and `on_exit` are lists of actions on output lines: `high`, `low`,
`toggle`, `pulse` (with `ms`). Every line raised by a state is lowered when that
state is exited — `on_exit` is for anything beyond that, and **the engine
guarantees the lowering itself** rather than trusting the graph to be written
correctly. A valve left open because a graph forgot an `on_exit` is not an
acceptable failure mode.

Output lines are physical pins; where a rig wants them to reach vstimd, daqd
bridges them to Virtual Trigger Lines. fsmd does not know the difference and
should not.

### Randomised timings, drawn on the device

Timings — `timeout`, `hold_ms`, pulse widths — are distributions, not numbers:

| `dist` | Fields | Use |
|---|---|---|
| `fixed` | `ms` | most states |
| `uniform` | `min_ms`, `max_ms` | VStim's `m_RandInterval_ms` |
| `exponential` | `min_ms`, `max_ms`, `mean_ms` | truncated exponential foreperiod — flat hazard, so the animal cannot time the go cue |
| `choice` | `ms: [...]`, optional `weights` | discrete SOAs, catch-trial timing |

The draw happens **on state entry**, on the device. The point is not saving
bytes; it is that a range is a property of the paradigm, not of trial 412, so a
paradigm with fixed ranges needs nothing pushed per trial at all.

**Reproducibility is not negotiable, and is handled twice over.**

1. **Seeded, derived per trial.** triald sends a session seed; the per-trial
   stream is seeded `mix(session_seed, trial_id)`. Derived rather than
   free-running means replaying trial 412 alone draws trial 412's numbers, and a
   link reset mid-session desynchronises nothing.
2. **Every realised duration is reported back.** The seed makes it reproducible;
   the report makes it *evidence*, which is triald's stance on records. The pair
   is also a free consistency check.

Implementation constraints, which exist for reproducibility rather than speed:

- A PRNG in the portable core — **xoshiro128\*\* or PCG32**, identical on all
  four build targets. Explicitly **not** Arduino's `random()`: that is the
  underlying libc `random()`, which differs between the AVR, Renesas and ESP-IDF
  cores, so the same seed would give different sequences per board.
- **No hardware TRNG**, though the i.MX RT1062 and ESP32 both have one.
  Unreproducible is the one property we must not have.
- Bounded integers by **Lemire's multiply-shift**, not `%`. triald's CLAUDE.md
  calls out VStim's `rand() % n` as biased and unreproducible; repeating that one
  layer down would be poor.
- The truncated exponential's inverse CDF is a **32-entry integer table with
  linear interpolation**, not `log()`. Integer-only so the drawn value is
  bit-identical across the board matrix and the native build — a float path would
  make the native simulator's numbers merely *close* to the firmware's, which is
  worse than useless for replay.

### Cancellation

triald already has `POST /api/trial/cancel` taking a `{reason}` and producing a
record with outcome `CANCELLED` — *"recorded rather than dropped, so a gap in the
numbering never has to be explained."* fsmd honours the same principle: a
cancelled trial reports like any other, with its state path and effective
durations up to the cut.

**Cancel is a forced transition, not an early return.** Every graph implicitly
gains a terminal state `__cancelled__` with outcome `CANCELLED`. Cancelling means
transitioning to it through the ordinary exit path — so every output line the
current state raised is lowered by the same code that lowers it on any other
transition. The valve closing on cancel is not a special case somebody has to
remember to write; it is the only path there is.

| Source | `cancel_reason` | Trigger |
|---|---|---|
| Experimenter | `host` | `cancel` command from triald via the bridge |
| Link loss | `link_lost` | heartbeat gap while idle or armed |
| Hardware abort | `abort_line` | a dedicated TTL input, always live, graph-independent |
| Runaway trial | `trial_timeout` | wall-clock cap on total trial duration |

The last earns its place: a graph is user data, and it can contain a state whose
timeout is absent and whose only exit condition never arrives, at which point the
trial hangs with outputs high. Validation checks reachability of a terminal state
at upload — triald "refuses rather than failing later" — but the runtime cap
stays anyway, because static analysis cannot distinguish "10 s foreperiod" from
"hung".

**The race resolves one way, explicitly: the first terminal decision wins, and
fsmd reports what actually happened.** A cancel arriving after the FSM has
already reached a terminal state gets the *real* outcome back, not a fabricated
`CANCELLED`. triald must cope with asking to cancel and being told `HIT`. That is
honest; the alternative is a record claiming a trial was cancelled when the
animal had already responded. A cancel for an unknown `trial_id`, or with no
trial in flight, is refused rather than acked.

**Graceful abort is a separate thing, and it is free.** A paradigm wanting a tidy
abort maps an input line to a terminal state of its own choosing in the graph,
with its own outcome and its own exit actions. The forced cancel above is the
*ungraceful* one that must work when the graph is wrong or the link is gone.
Both, not one.

---

## The wire protocol

USB CDC, **newline-delimited JSON**, with a sequence number and a CRC — as
specified in triald's PLAN.md. The `921600` figure there matters only where a
UART bridge is in the path; on native USB CDC (all four targets) the baud
parameter is ignored and throughput is the USB link's. `seq` covers
link-level retry; `trial_id` covers trial-level attribution. Different jobs, both
needed.

Full spec lands in `dev/PROTOCOL.md`. The shape:

### Host → device

| Message | Payload | Notes |
|---|---|---|
| `hello` | protocol version, session seed | Handshake. Device replies with board, firmware version, capabilities, line counts |
| `graph_begin` | `{graph_version, n_states, inputs, outputs, safe_levels}` | Opens an upload. Clears the pending graph |
| `graph_state` | one state | **One message per state.** Peak parse buffer is one state, never the whole document — see *Fitting on 32 KB* |
| `graph_end` | `{checksum}` | Validates the assembled graph and commits it, or refuses naming the fault. The live graph is untouched until this succeeds |
| `configure` | `{trial_id, graph_version, patch: {...}, start_source}` | The per-trial message. `patch` supplies `$name` substitutions and may be empty |
| `start` | `{trial_id}` | Only when `start_source` admits serial |
| `cancel` | `{trial_id, reason}` | |
| `ping` | | Heartbeat; the link-loss watchdog is armed off it |
| `state` | | Inspection: current state, line levels, uptime |

### Device → host

| Message | Payload |
|---|---|
| `armed` | `{trial_id, graph_version}` — **both**, because a graph edit that did not land would otherwise leave the device confidently running the old paradigm |
| `result` | `{trial_id, outcome, cancel_reason?, path: [{state_index, entered_us, duration_us, drawn_ms, exit: "timeout"\|"condition"\|"cancel", condition_index?}], reward_ms, seq}` |
| `event` | Asynchronous line changes, for monitoring. Off by default; never in the trial's critical path |
| `error` | `{code, message, context}` |
| `log` | Free text, rate-limited, never load-bearing |

**No trial runs that the device was not confirmed configured for** — the local
form of triald's `StartPermittable()`. `armed` is that confirmation and it is not
skippable.

**The device fails safe**: outputs to their configured safe level on watchdog
timeout, reset, link loss, or graph refusal. The safe level is per line and part
of the graph, because "off" is not always "low".

---

## Architecture

### The portable core

The engine, the codec and the protocol are **plain C++17 with no `Arduino.h`**,
no dynamic allocation, and no floating point in any path whose result crosses the
wire. Hardware appears only behind a HAL:

```c++
struct Hal {
  uint32_t micros();
  uint32_t read_inputs();               // one word, all lines, one sample
  void     write_output(uint8_t, bool);
  size_t   serial_read(uint8_t*, size_t);
  size_t   serial_write(const uint8_t*, size_t);
};
```

Everything above it is a pure function of `(graph, patch, seed, input word,
time)`. That is what makes the native build a real test of the firmware rather
than a parallel implementation of it — the same lesson as triald's
`SimulatedBehaviourSource` and its note that *the debug controls are not a second
code path*.

Capacities are compile-time constants per board (`FSMD_MAX_STATES`,
`FSMD_MAX_CONDITIONS`, `FSMD_MAX_LINES`, `FSMD_JSON_CAPACITY`), so a graph that
will not fit is refused at upload with a clear message rather than failing at
trial 300.

### Repository layout

One directory per concern under `core/`, and **the dependency runs one way down
this list**: `graph/` knows nothing of the machine, `machine/` nothing of trials,
and `protocol/` sits above all three. Includes are written the same way in both
builds -- `"graph/state.h"`, never a bare filename -- so a header reaching
sideways shows up in the diff rather than in a build failure two milestones
later.

```
fsmd/
├── README.md                  what it is, the Bpod acknowledgement, quickstart
├── BUILD.md                   devcontainer, Ubuntu, Fedora, WSL
├── LICENSE
├── platformio.ini             uno_r4_minima · teensy41 · esp32
├── CMakeLists.txt             the host build, for the tests
├── firmware/
│   ├── core/                  no Arduino.h, no malloc, no float on the wire
│   │   ├── config.h              capacities and the unit/index aliases
│   │   ├── graph/                what a paradigm declares
│   │   │   ├── state.h              a node, and why one gets left
│   │   │   ├── transition.h         the three-mask predicate, hold, edge/level
│   │   │   ├── output_action.h      what a state does to the output lines
│   │   │   └── state_graph.{h,cpp}  the pools, and validate()
│   │   ├── random/               timings drawn on the device
│   │   │   ├── rng.{h,cpp}                  xoshiro128** · Lemire bounds
│   │   │   └── random_distribution.{h,cpp}  the four distributions
│   │   ├── machine/              the scan loop. Knows nothing about trials
│   │   │   └── state_machine.{h,cpp}
│   │   ├── io/                   the pins, conditioned, before the machine sees them
│   │   │   └── input_conditioner.{h,cpp}  debounce · invert · enable
│   │   ├── trial/                the add-on that gives a run an outcome
│   │   │   ├── trial.h                  the .tdr codes, the wire contract
│   │   │   └── trial_runner.{h,cpp}
│   │   ├── protocol/             the wire. See dev/PROTOCOL.md
│   │   │   ├── crc16.{h,cpp}        CRC-16/CCITT-FALSE, and the accumulator
│   │   │   ├── framing.{h,cpp}      lines in, lines out, crc verified
│   │   │   └── json.{h,cpp}         reader and writer, no allocation
│   │   └── hal.h                 the whole hardware surface: seven functions
│   ├── hal/                   renesas_ra4m1.cpp · native.cpp · teensy41.cpp
│   │                          one file per board, each guarded by its own arch
│   └── src/main.cpp           board entry point, deliberately thin
├── emulation/                 the board under Renode, so firmware/hal/ has tests
│   ├── fsmd-uno-r4.repl       Renode's own board file, plus a USB boot shim
│   ├── fsmd.resc              loads the platform and the ELF
│   └── tests/                 Robot: pin map, port registers, timer ISR, a whole
│                              session over a real UART. Never timing
├── bridge/                    Python: serial ⇄ triald HTTP
│   └── src/fsmd/              codec, link, the triald client, `fsmd` CLI
├── graphs/                    example graphs — go/no-go, 2AFC, fixation task
├── tests/
│   └── core/                  mirrors firmware/core, group for group
│       ├── helpers.h             the graph builder the tests read as
│       ├── graph/ random/ machine/ trial/ protocol/
│       └── third_party/doctest.h
└── dev/
    ├── PLAN.md                this file
    ├── PROTOCOL.md            the wire contract
    └── HARDWARE.md            pinouts, wiring, per-board notes
```

Unit tests do **not** run through PlatformIO. The core is plain C++17 with no
`Arduino.h`, so CMake builds it on the host and `ctest` runs against it; board
builds go through PlatformIO and never see a test. Neither knows about the
other, which is what keeps the native build a real test of the firmware rather
than a parallel implementation of it.

### The bridge

An Arduino cannot POST to triald, and triald deliberately shed the hardware link
(`SerialBehaviourSource` came off its roadmap). So the translator lives here:
`fsmd` the host process opens the serial port, speaks the protocol, and calls
`POST /api/trial/next`, `/api/trial/outcome`, `/api/trial/cancel`. It also owns
retry, the CRC, reconnection, and turning a device `result` into an
`OutcomeReport`.

It is the natural place for the fields fsmd cannot know: `precise_fixation` comes
from the eye monitor and `frame_loss` from vstimd, and both can veto acceptance
on their own. The bridge merges them into the report, or leaves them at their
defaults on a rig without them.

### Fitting on 32 KB

The Uno R4 Minima (RA4M1, **32 KB SRAM / 256 KB flash**) is the target that sets
the design, so the budget is written down rather than hoped for. The graph is not
the problem; the **JSON** is. Two rules make it fit, and both improve the design
on every board:

**Never hold a DOM of the whole graph.** The upload is chunked — `graph_begin`,
one `graph_state` per state, `graph_end`. Peak parse buffer is one state (~300 B)
rather than a 6 kB document, and per-state CRC and retry granularity come free.
This is the single decision that makes the small board viable.

**Indices, not names, at runtime.** The bridge holds the graph, so `result`
reports state *indices*; names resolve host-side. On-device name storage becomes
optional rather than mandatory.

With names resolved to bit indices at upload and distributions in a shared pool:

| | Capacity (R4) | Each | Total |
|---|---|---|---|
| States | 32 | 12 B | 384 B |
| Conditions | 64 | 16 B — three masks, goto, flags, hold | 1024 B |
| Output actions | 64 | 4 B | 256 B |
| Distributions | 32 | 16 B | 512 B |
| Trial path record | 64 entries | 16 B | 1024 B |
| JSON scratch | one state | | ~512 B |
| Serial RX/TX | | | ~1.5 kB |
| Stack + core | | | ~2 kB |
| | | **≈ 7.5 kB** | **~23% of SRAM** |

> **Measured at M3, and this estimate was wrong by about a factor of three.**
> The real figure is **11 928 B of `.data`+`.bss` (36.4%)**, and **21 144 B
> committed (64.5%)** once the framework's 8 KB heap and the 1 KB main stack are
> counted. It fits, with ~11 KB unclaimed.
>
> Two things the estimate missed, both structural rather than sloppy. The device
> holds **two** graphs, not one — the live graph and the one an upload is
> staging, which is what makes a failed upload leave the running graph intact —
> and it keeps a full-line **retry cache** so a resent command can be answered
> without acting twice. Neither is optional. Add the USB stack, which is not
> ours, and the arithmetic above accounts for well under half of what is
> actually there.
>
> A quarter of the SRAM is a heap the portable core never allocates from;
> `BSP_CFG_HEAP_BYTES` is fixed at `0x2000` in the Arduino variant. Recovering it
> means patching the framework and is worth doing only if something needs the
> room. Full breakdown, and the reason the 1 KB declared stack is not the true
> headroom, in **`dev/HARDWARE.md`**.

A condition being three `uint32_t` masks and three bytes is why **TTL
combinations are cheaper than per-line edge bookkeeping**, not more expensive.
32 states and 64 conditions is a large paradigm; the Teensy gets 256/512 from the
same source with different compile-time constants, and an oversize graph is
refused at upload with a message naming what overflowed.

The path record is a ring buffer with an overflow flag: a graph can loop, and a
trial that visits 200 states must degrade to a truncated path rather than to a
corrupted one.

**What the R4 genuinely cannot do**: no Ethernet, so it always needs the host
bridge (true of all four targets); ~15 kB of headroom left, which rules out
on-device analogue buffering; and it is the target that refuses the largest
graphs first. None of that touches the trial loop.

### Board matrix

| Env | MCU | RAM | Notes |
|---|---|---|---|
| `uno_r4_minima` | Renesas RA4M1, 48 MHz | 32 KB | **Reference target — the only board we have.** Native USB CDC, hardware timer via `FspTimer`. Everything is developed and proven here first. See *Fitting on 32 KB* |
| `teensy41` | i.MX RT1062, 600 MHz | 1 MB | The headroom target, and Bpod's own board. Added once the R4 works; expected to be easy, since nothing in the design needs its resources |
| `esp32` | Xtensa LX6, 240 MHz | 520 KB | **WiFi and BT stay off in the trial loop** — triald's PLAN.md says so and it is right. Pin the engine to one core; be careful of flash operations stalling interrupts |
| `native` | host | — | Unit tests and the simulator, and the **first** thing that works. Not a toy: it runs the same `core/` |
| `linux_gpiochip` | Raspberry Pi etc. | — | **Not planned, but not a new project either.** See below |

**A Raspberry Pi fsmd would be a fifth HAL, not a second project.** Everything in
`firmware/core/` is plain C++17 with no `Arduino.h`, and the entire hardware
surface is seven functions. A Linux backend reads the input word from a gpiochip
line-request, writes outputs the same way, and takes `clock_gettime` for micros —
`native.cpp` is most of the way there already. Same graph format, same protocol,
same bridge, same golden vectors. The one real difference is that a Pi cannot
hold 10 kHz deterministically without `PREEMPT_RT` and careful isolation, which
is a note on that HAL rather than a change anywhere else. This is the strongest
argument for the portable core paying for itself.

Scan rate target: **10 kHz on all three boards**. At 48 MHz that is 100 us per
pass against a few hundred cycles of mask evaluation, so the RA4M1 has room —
*provided the HAL reads pins through direct port registers*. The Renesas core's
`digitalRead()` costs 1-2 us per call and would dominate the budget on its own.
That is a HAL requirement, not a board limitation, and it is why `read_inputs()`
returns the whole word rather than being called per line.

The achieved rate is measured at boot and reported in `hello`, so the host knows
the timing resolution it is actually getting rather than the one we hoped for.

It is not enough on its own, because the boot measurement is a *floor*: it
covers reading and conditioning the pins, and evaluating a state's transitions
is on top of it and depends on the graph. So every `state_report` also carries
`scan.overruns` and `scan.worst_gap` — scan periods that went by with no scan in
them, counted rather than absorbed. A board that quietly misses scans looks
exactly like a board that is fine, and a response window measured on a clock
that skipped is the failure this is here to make visible.

**The timer is a tick source, not the scan.** Bpod runs its state machine inside
the ISR and is right to; we cannot, because this firmware parses JSON in the
foreground, and an ISR advancing the trial while `loop()` is halfway through
`receive()` shares the runner, the graph and the reply buffer with it. The
alternatives are a critical section around every command — which stalls the scan
for as long as a parse takes, the very thing it was protecting — or a lock-free
handoff of every command the ISR must apply, which is a rewrite of the session.
So the ISR counts and `loop()` scans: correct by construction, and the engine
stays byte-identical to what the host tests exercise. The cost is jitter rather
than rate, and jitter is what `scan.overruns` counts. If a board says the link
stalls the scan too often, the fix is a command handoff in `firmware/src/
main.cpp` and nothing in `core/` moves. The reasoning is written out at the top
of that file.

---

## Testing

Test the wiring, not just the class — triald's rule, and the same seam works
here. The native build makes the engine testable at host speed with no board
attached.

- **Unit**, on `core/`: condition masks against a truth table; `hold_ms` restart
  on a dropout; declaration-order resolution; the PRNG against known vectors from
  the reference implementation; each distribution's bounds and mean; graph
  validation refusing unreachable terminals, oversize graphs, unknown line names,
  unknown outcomes.
- **Property**: a random graph plus a random input trace never leaves an output
  line high after a terminal state, and never fails to produce exactly one
  result per `configure`.
- **Golden**: a fixed seed and a recorded input trace produce a byte-identical
  `result` — this is the test that catches a reproducibility regression, and it
  runs on **every board**, not just native, since that is the whole claim.
- **Integration**: the bridge against the native core over a pty, whole trials,
  including cancel-races and link loss.
- **On-hardware smoke**, in CI where a board is attached: scan rate, jitter, and
  round-trip latency reported as numbers rather than pass/fail.

---

## What this changes in triald

**This needs an amendment to `triald/dev/PLAN.md`, not a silent divergence.**

That document says the MCU receives "channels, windows and a reward duration",
and places the branch structure — *"the millisecond values, the branch structure,
the outcome names"* — in triald as declarative data. fsmd pushes the branch table
down to the device as a graph.

The reason it is still compatible: **the firmware does not learn the trial
type.** A graph is a paradigm, not a condition, and the property PLAN.md is
protecting — firmware stable while paradigms change — is preserved as long as the
graph is uploaded rather than compiled in. The interval table's third row moves
from "held by triald" to "authored in triald, executed on the device", which is
a smaller change than it first appears, and the alternative is a network
round-trip per state transition, which is not available at these latencies.

triald keeps: the outcome taxonomy, acceptance, counters, rounds, sets, the
record. It gains: a `graph` field on the trial type, or a graph store alongside
the trial type store. **Open: which.**

---

## Open questions

1. **Where do graphs live in triald?** A field on `TrialType`, or a separate
   store addressed by name with the trial type referencing one? The latter fits
   "sets are addressed by name" and stops N trial types carrying N copies of the
   same paradigm. It is also more machinery.
2. **Who authors a graph?** JSON by hand is fine for three states and unpleasant
   for fifteen. A Python builder in the bridge is cheap; a visual editor in
   triald's web UI is not, and triald has a no-build-step, no-CDN rule.
3. **Global timers and counters — now the biggest open question.** I had
   deferred these. Reading the firmware weakens that: Bpod gives each of them a
   *dedicated transition matrix* (`GlobalTimerStartMatrix`, `GlobalTimerEndMatrix`,
   `GlobalCounterMatrix`) and ships 16 timers and 8 counters by default. They are
   first-class there, not bolted on, which is evidence that real paradigms need
   them — "house light off for 5 s regardless of state", "abort the block after 3
   consecutive errors". Counters arguably belong in triald, which owns counting.
   Timers do not: a timer spanning states cannot live on the slow bus. **Proposal:
   global timers in v1, counters deferred to triald.** Needs your call.
4. **Analogue inputs.** Lick detection is often capacitive or a beam-break, both
   digital, but a load cell or a photodiode is not. A threshold-crossing input
   would fold into the same input word with no change to the condition model.
   Out of scope for v1; the model already admits it.
5. **Timestamp alignment — provisionally answered by Bpod.** Take its
   `SyncMode`: 0 pulses the sync line on trial start and end, 1 toggles it on
   every state change, 2 free-runs at 10 Hz. Mode 1 makes the whole state path
   recoverable from the ephys recording alone, which is stronger than anything
   this plan had. Confirm mode 1 is what Bremen's rigs want as the default.
6. **License — settled and applied.** **Firmware GPLv3-or-later, bridge
   LGPLv3-or-later**, so an experiment importing the bridge is not placed under
   copyleft — the same split, and the same reason, as vstimd's client. `LICENSE`,
   `bridge/LICENSE`, `NOTICE`, and an `SPDX-License-Identifier` on every source
   file are in the tree.

   One correction to the reasoning this question was written with: it assumed
   "Bpod's firmware is GPLv3, so borrowing from it makes fsmd's firmware GPLv3."
   **Nothing was in fact borrowed.** Every mention of Bpod in these sources is a
   comment comparing our design to theirs, usually to explain a divergence; no
   Sanworks code is copied, translated or adapted, and no file carries a Sanworks
   copyright line. GPLv3 was therefore not compelled and is a deliberate choice
   to stay compatible with the system we take our ideas from. If Bpod source is
   ever incorporated, that file takes the Sanworks notice alongside ours and
   `NOTICE` says which file and what was taken.

7. **`start_source`.** Proposed as a per-trial field taking `ttl`, `serial` or
   `either` — TTL on the rig so reaction times need no clock sync, serial as the
   desk-testing path. Confirm.

---

## Milestones

| | | |
|---|---|---|
| **M0** | ✅ | Repo, `platformio.ini` with `native` + `uno_r4_minima`, `dev/PROTOCOL.md`, a native build that compiles and does nothing |
| **M1** | ✅ | Core engine — conditions, timers, RNG, the four distributions — unit-tested on native. No serial, no hardware |
| **M2** | ✅ | Protocol codec: chunked graph upload, `configure`/`armed`/`result`/`cancel`. Covered in-process by `test_host_link_session`, and end-to-end over a real UART peripheral under Renode rather than the pty this milestone first imagined |
| **M3** | 🔶 | **Uno R4 Minima HAL** — direct RA4M1 port-register reads, `FspTimer` ISR at 10 kHz, real pins. Builds, links and runs emulated; RAM measured — 11 928 B static, 21 144 B committed, see above and `dev/HARDWARE.md`. **The achieved scan rate still needs a board** |
| **M4** | ▶️ | Bridge to triald: a whole session on the R4, with `triald sim`'s simulated subject replaced by the real board. `bridge/` is empty |
| **M5** | ☐ | Example graphs, `dev/HARDWARE.md` with R4 pinout and wiring, virtual events and output overrides, sync line. `graphs/` is empty; `dev/HARDWARE.md` has the R4 line map already |
| **M6** | ☐ | Teensy 4.1 and ESP32 HALs; the golden reproducibility test green on all three boards |
| **M7** | ☐ | Packaging |

### Emulation, and what it can and cannot settle

`firmware/hal/` is the one part of this repo the host build cannot compile, so
until M3 it was covered by nothing. It now runs under **Renode**, whose own
platform files already describe the R7FA4M1A and the Uno R4 Minima: the pin map,
the port-register access, the timer ISR and the protocol over a real UART
peripheral are all tested in CI with no board attached. See
`emulation/README.md`.

That is worth having on its own evidence — it immediately found that the session
dropped the entry state's output actions, so an action on the first state never
reached a pin, which every host test missed.

**It settles nothing about timing.** Renode runs on virtual time against a
nominal MIPS figure, so a scan there takes exactly as long as it is told to.
Emulation makes the HAL *correct*; only a board makes the 10 kHz claim *true*.
That division is why the milestone below still stands as written.

**M3 is the milestone that decides whether this plan is right.** The RAM budget
and the 10 kHz claim are both arithmetic until a board runs them; if either is
wrong, it is better to find out at M3 than at M6. Everything before it is
hardware-independent and everything after it assumes those two numbers held.
