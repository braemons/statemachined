# statemachined — the wire protocol

> **Status:** specification. The codec is milestone M2 in [`PLAN.md`](https://github.com/braemons/statemachined/blob/main/dev/PLAN.md);
> the firmware side of it is being written against this document, not the other
> way round.

The link between the **bridge** (a host process) and the **device** (firmware on
a microcontroller). USB CDC, newline-delimited JSON, a per-line identifier and
a CRC, as [`PLAN.md`](https://github.com/braemons/statemachined/blob/main/dev/PLAN.md) specifies.

Everything above the framing is a consequence of two constraints that are worth
stating before the tables, because most of the odd-looking decisions below come
from one of them:

- **The device has 32 KB of SRAM.** No message may require the device to hold a
  document larger than a few hundred bytes. That is why both the graph upload
  and the trial result are _chunked_, and why the path comes back as arrays
  rather than objects.
- **The device is the timing authority and nothing else.** It reports what
  happened; it never interprets. There is no trial type on this wire, no
  acceptance decision, and no paradigm vocabulary — only a graph, timings, and a
  record of a run.

Throughout, examples are wrapped for readability and `"crc":"...."` stands in
for a value that depends on the rest of the line. **On the wire every message is
exactly one line.**

---

## 1. Framing

One JSON object per line, terminated by a single `\n` (0x0A). A `\r` immediately
before the `\n` is accepted and ignored, so a terminal program on the other end
does not break the link.

```
{"msg_type":"ping","message_id":41,"crc":"A3CE"}\n
```

**Rules**

|                    |                                                                                                                                |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| Encoding           | ASCII. A byte ≥ 0x80 anywhere in a line is a framing error. Non-ASCII text belongs in `log`, escaped as `\uXXXX`               |
| Line length        | At most `max_line` bytes including the `\n`. The device reports its own limit in `hello_ack`; the reference board's is **512** |
| Object depth       | At most 4. A conforming message never needs more                                                                               |
| Unknown members    | **Ignored**, on both sides. This is how the protocol gains fields without a version bump                                       |
| Unknown `msg_type` | Answered with `error` / `unknown_type`. Never silently dropped                                                                 |
| Member order       | Free, **except** `crc`, which is always last                                                                                   |

A line longer than `max_line`, or one containing no `\n` after `max_line` bytes,
is discarded up to and including the next `\n` and answered with `error` /
`too_long`. The receiver must not attempt to parse a truncated object: a
half-parsed `graph_state` is exactly the failure this protocol exists to
prevent.

### 1.1 The CRC

`crc` is a string of exactly four uppercase hex digits: **CRC-16/CCITT-FALSE**
(polynomial `0x1021`, initial value `0xFFFF`, no reflection, no final XOR) over
the bytes of the line **preceding** the literal `,"crc":`.

```
{"msg_type":"ping","message_id":41,"crc":"A3CE"}
└       covered by the CRC       ┘└not covered ┘
```

`crc` is required to be the final member precisely so that the receiver can find
it without parsing: scan **backwards** from the `}` for `,"crc":"`, CRC
everything before it, compare. That costs one pass and no buffer, which matters
on the device and costs the host nothing.

A message whose CRC does not match is answered with `error` / `bad_crc` naming
the `message_id` if one could be read, and is otherwise dropped. **It is never acted
on**, not even partially.

A CRC is not security and is not claimed to be. It catches the failure that
actually happens on a USB CDC link — a truncated or spliced line after a
re-enumeration — early enough that a corrupt graph is refused instead of run.

### 1.2 `message_id`

An unsigned 16-bit counter, **independent per direction**, incremented by one
for every line sent and wrapping through zero. It exists for link-level retry
and for nothing else; trial attribution is `trial_id`'s job, and the two are
never conflated.

> This field was called `seq` until M3, and the name was the problem: it
> advertised a _sequence_, and readers reasonably expected ordering from it.
> This protocol provides none — a gap is explicitly not an error, and nothing
> anywhere waits for a lower number to arrive first. What the field actually
> does is identify one line, so that a resend of it can be recognised as one.
> It is named for that job now. No bridge shipped with the old name; `proto`
> stays **1**.

- Every device reply to a host command carries `in_reply_to` — the `message_id` of the command
  it answers — in addition to its own `message_id`.
- Unsolicited device messages (`event`, `log`, and `result_*`) carry no `in_reply_to`.
- **Every host command gets exactly one reply**, including each message of a
  graph upload. That is what makes a retry decidable: the bridge resends when it
  did not get one, and needs no rule per message type.

- A `message_id` equal to **the one the device last answered** makes it repeat that
  answer verbatim and perform no action. So a host command is idempotent under
  retry and the bridge can resend blindly after a timeout instead of reasoning
  about whether the device got it. A re-executed `start` would run a second
  trial; this is the thing that prevents it.

  The memory is **one command deep**, not a window, because this is strict
  request/response with one command in flight. Deeper would mean storing that
  many complete replies — `max_line` bytes each — which on a 32 KB part buys
  nothing that the one-deep case does not already cover. An older duplicate is
  therefore re-executed rather than deduplicated; the bridge must not have two
  commands outstanding.

- A gap in `message_id` is **not** an error. USB CDC does not lose bytes in the middle
  of a session, and treating a gap as a fault would turn a cosmetic problem into
  a dropped trial. Gaps are reported in `state` for diagnosis.

---

## 2. Types and units

| Notation         | Meaning                                                                                                                                                                                                                                             |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `u8` `u16` `u32` | JSON number, unsigned, within the stated width                                                                                                                                                                                                      |
| `i8` `i32`       | JSON number, signed                                                                                                                                                                                                                                 |
| `hex64`          | **String** of 1–16 hex digits. Used for the session seed, because a 64-bit integer is not representable in a JSON number — a `double` silently loses the low bits, and a seed that silently changes is a reproducibility bug that nobody would find |
| `mask`           | `u32`, one bit per line, bit _n_ is line _n_                                                                                                                                                                                                        |
| `ms`             | Milliseconds, `i32`. Every duration a graph declares                                                                                                                                                                                                |
| `us`             | Microseconds, `u32`, the device clock. **Wraps every ~71 minutes**; differences are correct across the wrap, absolute values are not comparable between trials                                                                                      |

**Milliseconds on the wire, microseconds in the record.** A graph is authored in
milliseconds because that is the unit an experimenter thinks in; a run is
reported in microseconds because that is what the device actually measured. No
value crosses this wire as a float.

---

## 3. Host → device

### 3.1 `hello`

Opens a session. Resets the device to idle, clears any pending upload, and
drives every output to its safe level. It does not clear the committed graph:
reconnecting the bridge must not cost a re-upload.

```json
{
  "msg_type": "hello",
  "message_id": 0,
  "proto": 1,
  "seed": "0123456789ABCDEF",
  "crc": "...."
}
```

| Field   | Type    |                                                                                                                                   |
| ------- | ------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `proto` | `u16`   | Protocol version. **1**                                                                                                           |
| `seed`  | `hex64` | The session seed. Every per-trial stream is derived from it and the `trial_id`, so a session replays exactly from this one number |

Answered with `hello_ack`, or `error` / `bad_proto`.

### 3.2 The set upload

**A session uploads every graph it will use, once, before its first trial**, and
then switches between them with `configure`'s `graph_index` (§3.3). Nothing is
uploaded between trials. See `docs/developer/daemon.md` §3.2 for why: an upload that
happens only when the trial type _changes_ lengthens the ITI on exactly those
trials, which is a timing difference correlated with the variable under study.

Eight message types, in this order:

```
set_begin
  graph_dist        × n_distributions        (see below on where these may go)
  graph_begin       × n_graphs, in slot order
    graph_timer       × global timers, if any (set-scope; see below)
    graph_state       × that graph's n_states, in index order
      graph_transition  × this state's transitions
      graph_action      × this state's entry and exit actions
  graph_end
set_end
```

**`graph_transition` and `graph_action` attach to the most recently declared
`graph_state`, and a `graph_state` to the most recently declared
`graph_begin`.** That is not a convenience: the device stores states,
transitions and actions as one flat pool per kind, shared by every graph in the
set, and each level refers to its own as a `(first, count)` slice. A slice is
contiguous by construction only if everything belonging to it arrives together —
so the ordering rule on the wire _is_ the memory invariant, and a violation is
refused rather than producing a graph that owns somebody else's states.

**State indices are per graph**, counted from zero, because that is how the host
authored them: `entry`, a `timeout.target` and a transition's `target` all mean
"the n'th state of _this_ graph". The device adds the graph's offset on the way
in, and reports state indices the same way in `result_path` and `visit`. Every
other pool index — a distribution, in particular — is **set-global**, because
those pools are genuinely shared and one graph reusing another's foreperiod is
the point of sharing them.

> **A transition may not leave its own graph.** Selecting a graph by index has
> to select a _machine_. A shared pool makes crossing easy to write by accident,
> so it is refused at `set_end` with `bad_graph`.

**The upload is not staged, and this is the one thing that got worse.** A single
graph was double-buffered, so a failed re-upload left the previous paradigm
running. Two sets do not fit in 32 KB, so the device fills the live one: from
`set_begin` until `set_end` succeeds it holds **no graph at all**, `configure` is
refused with `not_ready`, and every output sits at its safe level (§3.5). That
fails safe and loudly where the alternative would be a board quietly running a
paradigm somebody thought they had replaced — and it can only happen between
sessions, since an upload is refused with `busy` while a trial is armed.

#### `set_begin`

```json
{
  "msg_type": "set_begin",
  "message_id": 1,
  "set_version": 7,
  "n_graphs": 3,
  "crc": "...."
}
```

| Field         | Type  |                                                                                                                                                                                                           |
| ------------- | ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_version` | `u16` | The host's identifier for this set. Echoed in `armed` and checked by `configure`                                                                                                                          |
| `n_graphs`    | `u8`  | Declared up front so an oversize set is refused before the first graph is sent, and so a `set_end` that arrives early is caught rather than committing a set with a hole in it. At most `caps.max_graphs` |

#### `graph_begin`

```json
{
  "msg_type": "graph_begin",
  "message_id": 4,
  "slot": 0,
  "n_states": 4,
  "entry": 0,
  "crc": "...."
}
```

| Field      | Type |                                                                                                                                                                                                                                  |
| ---------- | ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `slot`     | `u8` | Which graph in the set this is, and what `configure`'s `graph_index` will name. Must be the next one: stated rather than implied by arrival order, so a dropped `graph_begin` is a refusal rather than a silently renumbered set |
| `n_states` | `u8` | Declared up front so an oversize graph is refused before the first state is sent, not after the last                                                                                                                             |
| `entry`    | `u8` | State index the machine starts in, **within this graph**                                                                                                                                                                         |

> **`invert`, `enable`, `safe` and `debounce_ms` used to be here.** They describe
> the _wiring_ rather than the paradigm, and carrying them on `graph_begin` made
> "change the debounce" mean "re-upload the graph" — and, worse, left a board
> with no graph unable to fail safe correctly. They are now §3.5's `wiring`
> command. A device that receives them here ignores them, as it ignores any
> unknown member; it does not refuse the upload.

#### `graph_dist`

One entry of the distribution pool, which is shared by the **whole set**.
Timeouts and holds refer to it by index, so a graph reuses one distribution
everywhere it means the same thing, and two graphs reuse one where they mean the
same thing.

A host may send them all at set level, before the first `graph_begin`, or with
the graph that introduces them. The one rule is the one that has always been
here: a distribution arrives **before any state that could name it**, so a state
is range-checked against the pool as it arrives rather than after the fact.

```json
{
  "msg_type": "graph_dist",
  "message_id": 2,
  "i": 0,
  "kind": "uniform",
  "a": 300,
  "b": 700,
  "crc": "...."
}
```

| `kind`          | `a`           | `b`    | `c`     | `opts` / `weights`                                                   |
| --------------- | ------------- | ------ | ------- | -------------------------------------------------------------------- |
| `"fixed"`       | the value, ms | —      | —       | —                                                                    |
| `"uniform"`     | min ms        | max ms | —       | —                                                                    |
| `"exponential"` | min ms        | max ms | mean ms | —                                                                    |
| `"choice"`      | —             | —      | —       | `opts`: array of ms. `weights`: optional array of `u32`, same length |

`i` must equal the number of distributions already accepted. Stating it
explicitly rather than implying it from arrival order costs four bytes and turns
a dropped message from a silently mis-indexed graph into a refusal.

#### `graph_state`

```json
{
  "msg_type": "graph_state",
  "message_id": 6,
  "i": 1,
  "terminal": null,
  "timeout": { "dist": 0, "target": 2 },
  "crc": "...."
}
```

| Field      | Type             |                                                                                                                                                                                                                                                                                 |
| ---------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `i`        | `u8`             | State index. Must equal the number of states already accepted                                                                                                                                                                                                                   |
| `terminal` | `i8` or `null`   | The outcome code this state reports, or `null` for a non-terminal state. The device treats it as opaque — see §6                                                                                                                                                                |
| `timeout`  | object or `null` | `dist` indexes the distribution pool; `target` is the state entered when it expires                                                                                                                                                                                             |
| `relight`  | `u8` or `null`   | **Terminal states only.** The distribution the dwell in this state is drawn from — how long before another run may begin. Absent or `null` means none, which is what every graph written before this field existed says. A `relight` on a state that is not terminal is refused |

**`relight` does not give a terminal state an exit.** Nothing exits a terminal
state: the run ends there, its record is closed, and the dwell is drawn on
arrival and reported in that state's visit as `drawn_ms`. What it decides is
when the _next_ run may start, and whether anything acts on it is a property of
the device rather than of the graph — see §3.8. The same graph therefore runs
unchanged under a host that arms every trial itself, which is the point of
putting the timing here and the authority there: an inter-trial interval is a
paradigm decision that has to replay with the trial it followed, and who arms
trials is a fact about the deployment.

A terminal state that declares **no** dwell is where a self-driving board stops.
That is how a paradigm says "this outcome ends the session" — per outcome, which
a single device-wide setting could not express.

#### `graph_transition`

```json
{
  "msg_type": "graph_transition",
  "message_id": 7,
  "all": 3,
  "any": 0,
  "none": 8,
  "target": 2,
  "hold": 1,
  "level": false,
  "crc": "...."
}
```

| Field    | Type           |                                                                                                                                                                                                                             |
| -------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `all`    | `mask`         | Every one of these lines must be high                                                                                                                                                                                       |
| `any`    | `mask`         | At least one must be high. `0` means "don't care"                                                                                                                                                                           |
| `none`   | `mask`         | None of these may be high                                                                                                                                                                                                   |
| `target` | `u8`           | State entered when the predicate fires                                                                                                                                                                                      |
| `hold`   | `u8` or `null` | Distribution index for how long the predicate must stay true before it fires. `null` for none                                                                                                                               |
| `level`  | bool           | `false` (default): the predicate fires on its own **rising edge**, so a transition already true on entry does not fire until the predicate goes false and true again. `true`: it fires immediately on entry if already true |

Declaration order resolves a tie: the first transition of a state whose
predicate holds is the one that fires. Bpod does the same, and it is the only
tie-break an experimenter can reason about from reading the graph.

#### `graph_action`

```json
{
  "msg_type": "graph_action",
  "message_id": 8,
  "on": "entry",
  "line": 2,
  "kind": "pulse",
  "ms": 50,
  "crc": "...."
}
```

| Field   | Type                                                                             |                                                                                       |
| ------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `on`    | `"entry"` or `"exit"`                                                            | When it runs                                                                          |
| `line`  | `u8`                                                                             | Output line. Must be below the device's `n_output_lines`. Not sent by the timer kinds |
| `kind`  | `"high"` `"low"` `"toggle"` `"pulse"` `"timer_start"` `"timer_cancel"`           |                                                                                       |
| `ms`    | `u16`                                                                            | `pulse` only: how long it stays high. Must be non-zero. Ignored otherwise             |
| `timer` | `u8`                                                                             | `timer_start` / `timer_cancel` only: which global timer. Must be below `max_timers`   |

**`timer_start` and `timer_cancel` name a timer in `timer`, never a line in
`line`.** They are two different index spaces with two different sizes, and the
device bounds-checks them separately — a timer index validated against the
output lines would let a set name a timer that does not exist and then do
nothing at the moment it mattered. Refused as `bad_field` on `timer`.

A `timer_start` on a timer that is already running is ignored rather than
restarting it, so a state re-entered in a loop cannot keep pushing the same
timer's end further away. See `graph_timer` below.

**`pulse` is "high for at most `ms`", not "high for exactly `ms`".** It comes
down on the device's own clock, at scan resolution, or when the state is left —
whichever happens first. A pulse longer than its state does not outlive it. A
`pulse` with `ms` of 0 is refused as `bad_pulse`: it would ask for a line to
rise and fall in the same instant, and whether that ever reached a pin would
depend on when the scan landed.

**`toggle` is resolved against the device's own record of where the line is**,
which starts from the graph's `safe` levels at reset and follows every action
since. It is not a read-back of the pin. If something outside the graph drives
an output line, the device does not know and a subsequent `toggle` goes the
wrong way.

**All of a state's `entry` actions must arrive before its first `exit` action.**
The two are separate slices of the same pool and each has to be contiguous, so
interleaving them would silently give one slice the other's members. Refused as
`bad_order`.

**A terminal state's `entry` actions do run**, and they are how a reward is
written: `pulse` the valve line on entering `Hit`. The alternative — hanging it
off the exit of whichever state happened to precede the terminal one — spreads a
single intention across every route into it.

What the device cannot do is lower them _by exiting_, since nothing exits a
terminal state. A `pulse` still falls on its own width — the device keeps
servicing pulses after the trial has ended, so a reward closes itself. Anything
set `high` there **stays high until the next trial starts or a fail-safe runs**.

That asymmetry is the reason to write a reward as `pulse` rather than as `high`:
only one of the two comes down on its own.

**Every line a state drives high is driven low again when the state is left**,
by the device, whatever the exit cause and whether or not the graph said so. An
exit action is for what the graph wants _in addition_; a valve left open because
a graph forgot one is not a failure mode this protocol admits.

#### `graph_timer`

A timer that runs **beside** the state machine rather than inside it.

```json
{
  "msg_type": "graph_timer",
  "message_id": 9,
  "i": 0,
  "width": 2,
  "delay": 1,
  "gap": 3,
  "line": 0,
  "loops": 3,
  "none": 16,
  "crc": "...."
}
```

| Field         | Type   |                                                                                                                                                            |
| ------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `i`           | `u8`   | Timer index. Must equal the number already declared — timers arrive in order, so a set cannot leave a hole an action could name                             |
| `width`       | `u8`   | Distribution index: how long it stays high. **Required** — a timer that is never high is a line that never moves                                            |
| `delay`       | `u8`   | Distribution index: onset delay between trigger and first pulse. Bpod's `OnsetDelay`                                                                        |
| `gap`         | `u8`   | Distribution index: dead time between pulses when `loops` > 1. Bpod's `LoopInterval`                                                                        |
| `line`        | `u8`   | A real output line driven alongside the timer's own bit. Optional: a timer that only gates a transition needs no pin                                        |
| `loops`       | `u8`   | Pulses per trigger. Absent means 1. **`0` runs until something stops it**                                                                                   |
| `all` `any` `none` | `u32` | What triggers it — the same three masks, over the same word, as a `graph_transition`. All absent means it is started only by a `timer_start` action     |
| `active_low`  | `bool` | Drive `line` low while running rather than high. Applies to the pin only, never to the timer's own bit                                                      |
| `trial_bound` | `bool` | Stop when the run that started it ends. Absent means it runs on                                                                                             |

**A running timer is an input line that is high.** Timer _n_ holds input line
`first_timer_line + n` (from `hello_ack`'s `caps`) high for as long as it runs.
That is the whole design, and everything else follows from it:

- A transition waits on a timer exactly as it waits on a lever. "When the
  foreperiod timer ends" is `"none": <that line's bit>`; "while it is still
  running, and the left lever is down" is a combination. No event vocabulary
  exists for timers, on the wire or in the firmware, because none is needed.
- `hold` and `level` apply to a timer as they apply to any other line.
- **A timer can trigger another timer**, because the timers' own bits are in the
  word the triggers are evaluated against. Bpod's `OnsetTrigger` and VStim's
  divider chain, arrived at by writing nothing.

Timers are counted **down from the top** of the 32-line word so that real lines
grow up from zero and timers grow down from the end. `first_timer_line` is
therefore not derivable from `n_input_lines`, and a host must read it rather
than compute it — the point being that a graph means the same thing on a board
with eight input lines and one with twenty.

**Timers are set-scope, not graph-scope**, despite arriving inside a
`graph_begin` block. The nesting is about ordering only: a timer names
distributions, so it has to arrive after them. What it means is device-wide,
because a timer outlives the run that started it and a run belongs to one graph.

**Lifetime.** A timer keeps running when the trial that started it ends, unless
it declared `trial_bound`. That is what lets one raise a line while the device
sits between trials — which is the only way anything on a loopback rig can
produce the edge a `start: "line"` trial waits for (§3.4). A new trial's start
cancels whatever is still running, so a timer cannot leak into the run after
next, and `fail_safe` stops all of them.

**Accuracy.** Durations are whole milliseconds, served on the scan: an edge lands
on the first scan at or after its deadline, so it is late by less than one scan
period and never early. That lateness does **not** accumulate — each phase's
deadline is measured from the deadline just met rather than from the scan that
noticed it, so the Nth edge of a free-running timer is one scan late, not N. A
timer that falls a whole cycle behind resyncs and drops the missed cycles rather
than emitting a burst to catch up. See `docs/operations/hardware.md`, "Timer
accuracy".

There is no PWM. The line is driven high or low, never to an intensity.

**Triggering.** The trigger fires on the predicate's false→true edge, like a
transition, and never on the first word after a set is committed — there is no
previous word for it to have been an edge against. A trigger arriving while the
timer is already running is ignored.

#### `graph_end`

```json
{
  "msg_type": "graph_end",
  "message_id": 40,
  "n_transitions": 6,
  "n_output_actions": 5,
  "crc": "...."
}
```

Closes one graph. `n_transitions` and `n_output_actions` are **this graph's**
counts, not the set's, checked against the device's — a host that miscounted one
graph should be told which graph. Answered with `ack`.

#### `set_end`

```json
{
  "msg_type": "set_end",
  "message_id": 41,
  "n_states": 9,
  "n_transitions": 6,
  "n_output_actions": 5,
  "checksum": "<crc16>",
  "crc": "...."
}
```

The set's totals across every graph, and the checksum.

`checksum` is CRC-16/CCITT-FALSE accumulated over the **CRC-covered bytes of
every upload message since `set_begin`, in arrival order, `set_begin` included
and `set_end` excluded**. It is not the same thing as the per-line `crc`: that
one catches a corrupt line, this one catches a _missing_ one.

On success the whole set is validated — every index in range, every slice inside
its pool, no output line the board does not have, no transition leaving its own
graph, and per graph a terminal state reachable from the entry state with no
unreachable state — and then committed. **All of it or none of it**: a set that
uploaded four graphs and validated three is a session that fails at trial 40
instead of before the animal is in the booth. Answered with `set_ok` or `error`
/ `bad_graph`, whose `context` names what failed.

### 3.3 `configure`

The per-trial message. Arms the device for exactly one trial.

```json
{
  "msg_type": "configure",
  "message_id": 41,
  "trial_id": 193,
  "set_version": 7,
  "graph_index": 2,
  "cap_ms": 30000,
  "start": "serial",
  "patch": [{ "i": 0, "a": 250, "b": 900 }],
  "crc": "...."
}
```

| Field         | Type                         |                                                                                                                                                                                                                                                                      |
| ------------- | ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `trial_id`    | `u32`                        | The host's identity for this trial. Appears in `armed` and `result`, and a `cancel` for any other id is refused                                                                                                                                                      |
| `set_version` | `u16`                        | Must match the committed set. A set edit that did not land would otherwise leave the device confidently running the old paradigms                                                                                                                                    |
| `graph_index` | `u8`                         | Which graph of the set this trial runs. **This is the switch**: every graph is already on the device, so changing paradigm between two trials is one field on a message that was going to be sent anyway. Absent means `0`. Out of range is refused with `bad_index` |
| `cap_ms`      | `i32`                        | Wall-clock cap on the whole trial. `0` or absent means the device default. Validation cannot tell a 10 s foreperiod from a hang, so this stays regardless of the graph                                                                                               |
| `start`       | `"serial"` `"line"` `"both"` | What may start the trial once armed                                                                                                                                                                                                                                  |
| `start_line`  | `u8`                         | The input line whose **rising edge** starts the trial. Required when `start` admits `"line"`, ignored otherwise. Refused with `bad_index` if the board does not have that line, or if the wiring has it disabled                                                      |
| `timers`      | `u32`                        | Optional. Which global timers this trial runs, one bit per timer index. **Overrides the device's mask for exactly this trial** and is reverted when it ends, like `patch`. Absent leaves the device's own in force  |
| `patch`       | array                        | Optional per-trial overrides of distribution parameters, by pool index. Only `a`, `b`, `c` may be patched; `kind` may not. Reverted when the trial ends                                                                                                              |

`timers` is to a timer what `graph_index` is to a graph: the field a trial type
uses to select one, on a message the device was going to receive anyway, so
mapping a trial type onto a set of timers costs nothing in the inter-trial
interval. It is an override rather than a setting for the same reason `patch` is
— a mask that outlived its trial would be a timer running, or not running, that
nobody could account for afterwards. The device-level mask is `timers` (§3.6).

VStim carries the same two things separately: `m_TimerActive` switches a timer
on for the rig, and `m_StimList` says which trials it takes part in.

`patch` indices are into the set's shared distribution pool, like every other
distribution index. It is why a set does not have to be re-uploaded when only
the timings change, which is the common case. It cannot change the _shape_ of
anything — `kind` may not be patched, because that would be a different graph
and a different `set_version`.

**The override lasts exactly one trial.** The device keeps the values it
replaced and puts them back when the trial ends, when another `configure`
replaces the patches, and whenever a session resets: a patch that outlived its
trial would be a timing nobody could account for afterwards. A patch entry
naming a distribution that does not exist is refused with `bad_index`, and a
malformed one applies **nothing** — a trial running with half a patch on it is
not a state this protocol admits.

Answered with `armed`, or `error`.

### 3.4 `start`, `cancel`, `ping`, `state`

```json
{"msg_type":"start","message_id":42,"trial_id":193,"crc":"...."}
{"msg_type":"cancel","message_id":43,"trial_id":193,"reason":"host","crc":"...."}
{"msg_type":"ping","message_id":44,"crc":"...."}
{"msg_type":"state","message_id":45,"crc":"...."}
```

`start` is refused unless the device is armed for that `trial_id` and the
configured `start` source admits serial. **No trial runs that the device was not
confirmed configured for.**

#### Starting on a line

A trial armed with `start` of `"line"` or `"both"` begins on the **rising edge**
of `start_line` — the scan at which that bit is set having been clear on the
scan before.

An edge and not a level, because the alternative is unusable: a line still
asserted from whatever came before would start the trial on the first scan after
`configure`, which is the host's timing rather than the subject's. So a line that
is already high when the trial is armed starts nothing until it is released and
asserted again.

The bit is read from the **conditioned** word, so `invert` has been applied and a
debounce, if that line has one, has already been waited out. It is the same edge
the paradigm's own transitions would see.

The device announces it with an unsolicited `started` — the same message the
serial path replies with, minus `in_reply_to`:

```json
{"msg_type":"started","message_id":88,"trial_id":193,"at_us":41902133,"by":"line","line":3,"crc":"...."}
```

`by` is `"line"` or `"serial"`, on both forms, so a host reads one field rather
than inferring the answer from whether the message carried an `in_reply_to`.

**On this form `at_us` is the start of the trial**, not an acknowledgement of
anything: it is the timestamp of the scan that saw the edge, which is the
timestamp that same scan stamped the entry state's `entered_us` with. The edge,
the timestamp and the pins the entry action drives are one call in the scan, so
the latency from pin to first output is one scan period and nothing else. (The
serial form's `at_us` means something weaker; see the note below.)

One edge starts one trial. The arming is spent by the edge that uses it, so the
line going high again mid-trial is an ordinary input like any other, and the next
trial waits for its own `configure`. A link lost disarms a trial that is still
waiting, so a board does not start a run for a host that has gone.

A trial armed on a line that never rises simply waits. There is nothing to time
out against on the device — `cap_ms` caps the trial, and the trial has not
begun — so the host owns that deadline. Sending another `configure` re-arms and
replaces it.

`cancel` takes a `reason` of `"host"`, `"link_lost"`, `"abort_line"` or
`"trial_timeout"`; only `"host"` is legal from the host, the others being the
device's own. A cancel for an unknown `trial_id`, or with no trial in flight, is
**refused rather than acked** — the bridge learns that nothing was cancelled.

A cancel arriving after the machine has already reached a terminal state gets the
**real outcome** back, not a fabricated `CANCELLED`. The first terminal decision
wins and the device reports what actually happened. The bridge must cope with
asking to cancel and being told `HIT`; the alternative is a record claiming a
trial was cancelled when the animal had already responded.

**`started` is an acknowledgement, and `at_us` is when the command was accepted
— not when the trial began.** The run begins on the device's next scan, so that
the timestamp the trial is stamped with and the pins its entry action drives are
the same instant. Those used to be up to a millisecond apart, and every trial's
first state reported a millisecond it had not spent; see
[`hardware.md`](../operations/hardware.md), "Response latency and duration
accuracy". A host that needs the trial's own clock reads `entered_us` on the
first row of the result (§4.3), which is the timestamp the machine actually ran
on. `at_us` remains what it always was, and is still the right thing to join a
host-side log against — it is the device's clock at the moment the command
landed.

A `state_report` asked for inside that window answers `running: true` with
`current_state` at the graph's entry state, agreeing with the `started` it
follows rather than with the engine, which has not ticked yet.

`ping` arms the link-loss watchdog. `state` asks for `state_report` and is for
inspection only — it is never in a trial's critical path.

### 3.5 `wiring`

What is _wired to the box_, as opposed to what a paradigm does with it. Sent
when a rig is wired and then not again for a year.

```json
{
  "msg_type": "wiring",
  "message_id": 9,
  "invert": 0,
  "enable": 4294967295,
  "safe": 0,
  "debounce_ms": [0, 2, 2, 0],
  "crc": "...."
}
```

| Field         | Type           |                                                                                                                                         |
| ------------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `invert`      | `mask`         | Lines read active-low. Opto-isolated inputs routinely are                                                                               |
| `enable`      | `mask`         | Lines that participate at all. Default all ones                                                                                         |
| `safe`        | `mask`         | Output levels on watchdog timeout, reset, link loss or a refused graph. Per line, because "off" is not always "low"                     |
| `debounce_ms` | array of `u16` | Per input line, index = line. May be shorter than the line count; **lines it does not name are set to 0**, so a debounce can be removed |

Every member is optional and an absent one leaves that setting alone —
`{"safe":5}` changes the safe levels and nothing else. `debounce_ms` is the
exception to "leaves it alone" only in the sense above: sending the array at all
replaces the whole table.

Answered with `ack`, or `error`. **Refused with `busy` while a trial is armed or
running**, like a graph upload and for a sharper reason: the conditioning it
changes is read by the scan, so a debounce edited under a running trial would
move a timing nobody could account for afterwards.

The wiring is read into a copy and installed whole, so a message that turns out
to be malformed halfway through leaves the board wired the way it was. A typo in
a debounce must not take the safe levels with it.

> **It survives a power cycle only if it is saved.** §3.9's `save` writes the
> wiring to the board's own storage, and the next boot reads it back before it
> drives a single line — which is what makes a rig with an active-low valve
> driver fail safe to _its_ levels rather than to all-low. A board that has
> never been saved to, or whose store is blank or damaged, comes up on the
> compile-time `STATEMACHINED_SAFE_LEVELS` instead, which is why that constant
> has to stay correct on its own. `hello_ack`'s `has_wiring` is how a host tells
> a configured board from one running defaults.

### 3.6 `timers`

```json
{ "msg_type": "timers", "message_id": 46, "enable": 5, "crc": "...." }
```

| Field    | Type  |                                                      |
| -------- | ----- | ------------------------------------------------------ |
| `enable` | `u32` | One bit per global timer index. `1` means it may run   |

The device-level mask, which holds **between** trials — where a free-running
timer is doing most of its work, and which a per-trial field could not describe.
A single trial overrides it with `configure`'s `timers` (§3.3).

Answered with `ack` carrying `enable` and `n_timers`, so a host never has to
infer what took: a mask naming timers the committed set does not declare is
accepted and simply has no effect, and the reply says what the device holds.

**Disabling a timer that is running stops it and puts its line back**, rather
than letting it finish. A timer holding a line up that the new configuration
does not account for is not something a host can plan a trial around, and "it
will drop in another 400 ms" is not an answer.

Refused as `busy` while a trial is armed or running, exactly like `wiring` and
for the same reason: it changes what the scan does, and a timer switched off
under a running trial would move a timing that trial's record could not account
for.

### 3.7 `pins`

```json
{ "msg_type": "pins", "message_id": 9, "dir": "in", "crc": "...." }
```

Asks the device what its lines are called and, by asking twice, which of them
are inputs and which are outputs. Answered with §4.6's `pin_map`, or with
`error` / `no_pin_map` by a build that has no pins worth naming.

**Why this is on the wire at all.** Which pin a line is, and which direction it
has, are fixed when the firmware is compiled: the HAL's `kInputPins` and
`kOutputPins` are what `init()` calls `pinMode()` over, and no command changes
either. A host therefore cannot _derive_ the map, and the only alternative to
asking is keeping a copy of the board's table keyed by the `board` string — a
hand-copied pin map, which is the failure the RA4M1 HAL refuses to risk with the
Arduino core's table and which is no safer one layer up. A host that guesses
wrong drives a valve from a lever's line number and nothing anywhere says so.

| field |                                                                         |
| ----- | ----------------------------------------------------------------------- |
| `dir` | **Required.** `"in"` or `"out"`. Anything else is `error` / `bad_field` |

**One direction per request, and no chunking.** Both directions in one reply do
not fit `max_line` on a 32-line board, and a reply that silently carried half
the map would be worse than none: the host would believe it had the whole
thing. One direction always fits. A device whose labels somehow do not answers
`error` / `too_long` rather than truncating.

`pins` is legal whenever `hello` has been answered, changes nothing, and may be
asked at any time — including during a trial, though a host with any sense asks
once per connection.

### 3.8 `autorun`

```json
{
  "msg_type": "autorun",
  "message_id": 10,
  "enabled": true,
  "graph_index": 0,
  "cap_ms": 30000,
  "seed": "0123456789ABCDEF",
  "first_trial_id": 1,
  "crc": "...."
}
```

Who starts the trials. Everywhere else in this protocol the answer is the host:
triald is the decision authority, it chooses the trial type and arms each trial,
and the device is the timing authority that runs the one it was given. This is
the case where there is no decision authority at all — a board on a bench, an
unsupervised shaping session, a box with nothing plugged into its USB port — and
the device starts each run itself, taking the interval between them from the
dwell the terminal state it just reached declared (§3.2, `relight`).

| Field            | Type      |                                                                                                                                                                                                                                                                    |
| ---------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `enabled`        | bool      | Whether the device may start runs on its own. **Absent means this message is a question**: nothing changes and the current settings are reported                                                                                                                   |
| `graph_index`    | `u8`      | Which graph of the committed set it runs. Autorun cannot switch paradigms: switching is a decision, and the premise here is that nothing is making decisions                                                                                                       |
| `cap_ms`         | `i32`     | Wall-clock cap per run, as `configure`'s. It matters more here — nobody is watching for a graph that has hung                                                                                                                                                      |
| `seed`           | hex `u64` | The stream every autorun trial's randomness is derived from. Carried here rather than taken from the session, because a self-driving board may never receive a `hello` and so may never be given one                                                               |
| `first_trial_id` | `u32`     | Where the ids it assigns start, so a host can tell runs the board made on its own from its own                                                                                                                                                                     |
| `start_now`      | bool      | Default `true`. `false` records that this board should drive itself **without starting it** — which is how a rig is set up, because §3.9's `save` is refused on a board that is running and a board arming its own trials is never idle. Enable, save, power cycle |

Answered with `autorun_ok`, carrying `enabled`, `active`, `graph_index`,
`cap_ms` and `next_trial_id`. **`enabled` and `active` are not the same fact:**
the setting is stored and survives, while `active` is whether the board is
driving trials right now.

Refused with `not_ready` if no set is committed, `bad_index` if the graph does
not exist, and `busy` if a _host-driven_ trial is running — a message that hands
the board a new job must not land while it is doing one. `configure` is refused
the same way while autorun is active: one authority at a time, and disabling
autorun first is one message that says which of the two is in charge.

**Turning autorun off is never refused as busy.** "Stop" is the thing somebody
most wants while it is running, and it is the one command that must not be
refused for the reason that it is running. The run in flight ends through the
ordinary exit path, exactly as a `cancel` does: its lines come down and its
result is reported. A cancelled run reached no terminal state, so it drew no
dwell, so nothing follows it — the board stops without that being a separate
rule.

**A host that greets takes the rig.** `hello` stops a self-driving board — the
run in flight is cancelled through the ordinary exit path, so its lines come
down and its result is still reported — while leaving the stored setting alone,
so the next boot still comes up self-driving. Starting again takes another
`autorun`. That asymmetry is deliberate: a daemon that crashed must not be able
to leave a board delivering rewards to an animal nobody is watching, and the one
path into unattended running that no host asked for is a boot from settings
somebody deliberately saved.

**Losing the link does not stop it.** For a board that was explicitly told to
drive itself, the port closing is the expected end of "upload a paradigm, then
detach" rather than a fault: nothing is cancelled and nothing fails safe. A
board that was _not_ told to drive itself behaves exactly as it always has — the
trial is cancelled as `link_lost` and every line goes to its safe level.

### 3.9 `save`

```json
{ "msg_type": "save", "message_id": 11, "crc": "...." }
```

Write what the device currently holds — its wiring, its committed graph set and
its autorun settings — to the board's own storage, so that all three survive a
power cut. It takes no fields: what is saved is what is there, because a save
that took its own copy of the settings would be a second place for them to
disagree.

Answered with `saved`:

```json
{
  "msg_type": "saved",
  "message_id": 12,
  "in_reply_to": 11,
  "has_set": true,
  "set_version": 7,
  "autorun": true,
  "write_count": 3,
  "written": true,
  "crc": "...."
}
```

`write_count` is how many times this board's store has been written. It is
reported because data flash wears out — about 100,000 erase cycles on the
reference board — and a rig a third of the way through that budget should be
able to say so rather than failing one day without warning.

**A save that would store what is already stored writes nothing**, answers
`"written": false`, and leaves `write_count` where it was. The device compares
before it writes — the record is encoded again and matched against the stored
bytes as it goes, a few bytes at a time, with no second copy of the graph set —
because an erase cycle spent to change nothing is an erase cycle spent, and
pressing a save button twice must not cost one. It is answered rather than
refused: "it is already saved" is a success, and a caller should not have to
tell the two apart to know its settings are safe.

Refused with `not_ready` on a board with nowhere to keep settings, `busy` while
a trial is running (erasing and programming data flash blocks for tens of
milliseconds against a 100 µs scan, and refusing is better than quietly costing
somebody's response window its timing), and `storage` if the write did not land
— in which case the store holds **no** valid record, which the next boot reads
as "this board has forgotten" rather than as something wrong.

**At boot** the device reads the record back before it drives a single line. A
stored wiring's output safe levels are therefore the ones the first fail-safe
uses, which is what stops a rig with an active-low valve driver from opening it
on every power cycle. A stored set is _validated_, not merely trusted: the CRC
says the bytes are the ones that were written, not that they were a graph worth
running. And if the stored autorun says so, the board comes up running trials
with no host in the picture at all.

A blank store, a damaged one and a board with no store are all ordinary: the
compiled-in defaults stand, because a mitigation that depends on somebody having
saved settings is not one.

---

## 4. Device → host

### 4.1 `hello_ack`

```json
{
  "msg_type": "hello_ack",
  "message_id": 0,
  "in_reply_to": 0,
  "proto": 1,
  "board": "uno_r4_minima",
  "fw": "0.1.0",
  "n_input_lines": 8,
  "n_output_lines": 8,
  "scan_hz": 10000,
  "has_set": true,
  "set_version": 7,
  "n_graphs": 3,
  "has_wiring": true,
  "caps": {
    "max_line": 512,
    "max_states": 32,
    "max_transitions": 64,
    "max_output_actions": 64,
    "max_distributions": 32,
    "max_choice_options": 32,
    "max_path": 255,
    "max_graphs": 20,
    "max_timers": 8,
    "first_timer_line": 24
  },
  "crc": "...."
}
```

`scan_hz` is **measured at boot, not declared**, so the host knows the timing
resolution it is actually getting rather than the one the design hoped for.

`caps` holds the device's compile-time capacities. The bridge checks a graph
against them before uploading, which turns "refused at `graph_end`" into
"refused before the first byte" — a better error at no cost.

`first_timer_line` is the odd one out: it is not a capacity but a layout, and it
is here because nothing else can tell a host where the timers' bits are. Global
timer _n_ holds input line `first_timer_line + n` high while it runs (see
`graph_timer` in §3.2), and the timers are counted **down from the top** of the
32-line word so that real lines grow up from zero and timers grow down from the
end. That is deliberate: a base relative to a board's own `n_input_lines` would
silently renumber every timer when a graph moved between a board with eight
input lines and one with twenty.

They are **read, never assumed**, and `max_path` is the one where that has
already mattered: the reference board briefly shipped two images, one of which
carried a demo paradigm and therefore a second path buffer, and reported 64
where the other reported 255. There is one image now and it reports 255 — which
is exactly why a host reads the number rather than knowing it.

They are **nested rather than flat**, and that is a memory decision rather than a
stylistic one: a receiver's per-message member limit is what bounds how much
stack a parse costs, and flattening these would push the largest message in the
protocol past a limit that every other parse would then pay for.

`has_wiring` says whether anybody has sent §3.5's `wiring`. **False means the
board is running its compile-time defaults**, which is a different thing from
"wired the way this rig needs" and is exactly what a host must not have to
guess.

`has_set`, `set_version` and `n_graphs` say whether a set survived the reconnect, so a
bridge that dropped its link knows whether it has to re-upload.

### 4.2 `armed`

```json
{
  "msg_type": "armed",
  "message_id": 1,
  "in_reply_to": 41,
  "trial_id": 193,
  "set_version": 7,
  "graph_index": 2,
  "crc": "...."
}
```

Both fields, always. This is the confirmation that `start` requires and it is
not skippable.

### 4.3 The result

**The result is the record.** §4.4's `visit` stream reports the same visits as
they happen and is a _preview_: a host reconciles what it streamed against what
arrives here, and where the two disagree, this wins. The one case the stream is
the only complete copy is a `truncated` result, and `first_seq` below is what
lets a host say exactly which visits it is missing.

Chunked, for the same reason the upload is: a full path does not fit in a
512-byte line, and buffering one that did would cost the device a kilobyte it
does not have.

```
result_begin
  result_path  × ⌈path_len / batch⌉
result_end
```

```json
{"msg_type":"result_begin","message_id":9,"trial_id":193,"outcome":1,"cancel_reason":0,
 "total_us":1483200,"path_len":5,"first_seq":0,"total_visits":5,"truncated":false,"crc":"...."}

{"msg_type":"result_path","message_id":10,"trial_id":193,"from":0,
 "p":[[0,"timeout",255,500,0,500120],[1,"transition",2,0,500120,183044]],"crc":"...."}

{"msg_type":"result_end","message_id":12,"trial_id":193,"checksum":"<crc16>","crc":"...."}
```

Each entry of `p` is a fixed six-element array, **not** an object:

```
[state_index, exit_cause, transition_index, drawn_ms, entered_us, duration_us]
```

| Position | Type   |                                                                                                                                                                                                                                                                                           |
| -------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0        | `u8`   | Which state. An **index**, not a name — the bridge holds the graph and resolves names host-side, which is part of what keeps the device inside 32 KB                                                                                                                                      |
| 1        | string | `"timeout"` `"transition"` `"cancel"` `"terminal"`                                                                                                                                                                                                                                        |
| 2        | `u8`   | Which of **this state's** transitions fired, counted from zero in declaration order, or `255` for an exit that was not one. Per state, like `state_index` is per graph: the host reads the graph the way it wrote it, and never has to know where a state's slice of the shared pool sits |
| 3        | `i32`  | The **realised** duration of the draw, in ms. Reported so a random timing is evidence in the record and not merely reproducible from the seed                                                                                                                                             |
| 4        | `u32`  | Entry timestamp, device clock                                                                                                                                                                                                                                                             |
| 5        | `u32`  | Measured duration. This is what actually happened; position 3 is what was asked for                                                                                                                                                                                                       |

Objects would be clearer to read and roughly twice the bytes. The array form is
documented once, here, and decoded once, in the bridge.

`truncated` is set when the run visited more states than `max_path` holds. **A
graph may loop, and a long trial degrades to a truncated path rather than to a
corrupt one.** The record is a genuine ring and it drops from the **front**: the
interesting part of a trial is the response at the end, so overflow costs the
oldest visits, not the newest.

| Field          | Type  |                                                                                                                         |
| -------------- | ----- | ----------------------------------------------------------------------------------------------------------------------- |
| `path_len`     | `u8`  | How many entries `p` will carry in total — the size of the window, not of the run                                       |
| `first_seq`    | `u32` | The `seq` (§4.4) of the oldest visit still in that window. `0` unless the ring wrapped                                  |
| `total_visits` | `u32` | How many visits the run actually made. `total_visits > path_len` is what `truncated` means, and now it says by how much |

`from` on a `result_path` chunk stays an offset into what is being **sent**, not
into the run: chunk 0 starts at `first_seq`, whatever that is.

`result_end`'s `checksum` accumulates over `result_begin` and every
`result_path`, exactly as `graph_end`'s does, and catches a dropped chunk.

### 4.4 `visit`

One completed state visit, sent as it happens. Unsolicited, so it can arrive
between a command and its reply and in the middle of a result.

**"As it happens" means the visit is *recorded* as it happens and formatted a
moment later.** The trial loop copies it into a ring and returns; the line is
built and sent from the device's foreground. That is not a nicety: building one
of these lines costs ~120 µs, and doing it inside the trial loop — which on a
board is the timer interrupt — made the scan overrun its own tick and doubled
the board's response latency. See [`hardware.md`](../operations/hardware.md),
"Response latency and duration accuracy". Nothing about the *content* moves:
`entered_us` and `duration_us` are the machine's own timestamps, taken when the
visit happened, not when it was sent.

The visits of a run are always sent **before** the `result` that summarises
them.

**A visit may be dropped, and the drop is counted rather than hidden.** If the
ring fills — a burst of state changes faster than the link can be fed — the
newest visit is discarded and `scan.visits_dropped` in `state_report` (§4.5)
counts it. A host detects the gap in `seq`, which is what `seq` is for. **This
never costs a record**: `result_path` is built from the device's own account of
the run and not from this stream, so a dropped visit costs a live trace and
nothing else.

```json
{
  "msg_type": "visit",
  "message_id": 57,
  "trial_id": 193,
  "seq": 2,
  "v": [1, "transition", 2, 0, 500120, 183044],
  "crc": "...."
}
```

`v` is **the same six-element array as a `result_path` entry**, in the same
order and with the same types (§4.3). It is decoded by the same function on the
host; two shapes for one fact is how the two drift apart.

| Field      | Type  |                                                                                                                                                                                                                                                              |
| ---------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `trial_id` | `u32` | The id from `configure`. **`0` when there was no host-configured trial** — a line-started run before anything assigned an id. A board arming its own trials assigns them itself (§3.8), so those carry a real id. The trace is still worth having either way |
| `seq`      | `u32` | The visit's ordinal within the run, from `0`. A gap is what makes a dropped visit **detectable** rather than a hole nobody notices                                                                                                                           |

**Emitted when the state is left, not when it is entered**, because a visit's
duration and exit cause do not exist before then. For a trace that is not a
latency problem — what matters is the timestamp, and `entered_us` is exact — and
for a live display it costs one state of lag on a trial's first state only:
after that, each exit says both when the reported state ended and, via
`transition_index` resolved against the graph the host holds, which state the
machine is in now.

It is called `visit` and not `transition` for two reasons. `transition` is
already this wire's noun for an edge in a graph (`graph_transition`), and what
is reported is a completed _visit_ that happens to carry the transition which
ended it.

**Always on.** Unlike `event`, which can fire every scan, this fires a handful
of times per trial, and there is no rig configuration in which one would rather
not have the record. `state_report`'s `tx_stalls` is what would say otherwise.

### 4.5 `event`, `error`, `log`, `pong`, `state_report`

```json
{"msg_type":"event","message_id":13,"us":1483200,"word":6,"crc":"...."}
{"msg_type":"error","message_id":14,"in_reply_to":41,"code":"bad_graph","message":"...","context":"...","crc":"...."}
{"msg_type":"log","message_id":15,"level":"warn","message":"...","crc":"...."}
{"msg_type":"pong","message_id":16,"in_reply_to":44,"up_us":90210000,"us":1483200,"crc":"...."}
```

`event` reports the conditioned input word on change. **Off by default and never
in a trial's critical path**: it is a monitoring aid, and a link that cannot keep
up drops events rather than delaying a scan.

`pong` carries two clocks and they are not interchangeable. `up_us` counts from
the first time anything asked the device the time, so its origin differs every
session and it is for reading, not arithmetic. `us` is the **device clock
itself**, raw and wrapping every ~71 minutes — the same clock a result's
`entered_us` is in, which is what makes a `ping` round-trip usable to correlate
the two clocks at all.

`log` is free text, rate-limited, and never load-bearing. Nothing in the bridge
may parse it.

`state_report` answers `state` with the current state index, uptime, the
nested `graph` object (`has_set`, `set_version`, `n_graphs`, `index`),
`has_wiring` as in §4.1, `autorun` — whether the board is arming its own trials
(§3.8) — the counts of dropped and unusable lines, and a nested `scan` object.

The `scan` object also carries `timers_enabled` and `timers_running`. They are
in there rather than at the top level because the top level of `state_report` is
at the reader's member limit exactly, and `scan` is the object that already
holds what the scan is doing. `timers_enabled` is the mask **in force now**, so
during a trial that overrode it this is the trial's mask and not the device's.
`timers_running` is one bit per timer actually in flight, onset delay included —
a timer counting down its delay is running, it is simply not high yet.

`link_state` is what the session is doing: `0` greeting (nothing but `hello` is
answered), `1` idle, `2` armed, `3` running, `4` relighting — the dwell between
two self-driven runs, which only a board driving itself is ever in. Nested because a message is capped at sixteen top-level members
and that cap is what bounds the reader's stack footprint. Diagnosis, not
control.

```jsonc
"io":   {"in": 5, "out": 128},
"scan": {"hz": 9871, "overruns": 4, "worst_gap": 2, "tx_stalls": 0}
```

`io.in` is the _conditioned_ input word as of the last scan — after invert,
enable and debounce — and `io.out` is the device's own record of the output
levels, not a read-back: nothing can read a pin. Together they are the only way
anything outside the device can check that a graph's line numbers reach the pins
somebody wired.

`hz` is what the device measured of itself at boot, not a declared figure — and
it is a _floor_, covering reading and conditioning the pins but not evaluating
a graph's transitions. `overruns` counts scan periods that went by with no scan
in them since boot, and `worst_gap` is the most ever missed in a row.

`tx_stalls` counts the times a reply had to wait for the wire because the
device's outbound queue was full. Replies are queued and the link drained
without blocking, so this is the one remaining way the link itself can cost the
device a scan; a non-zero count means a burst outgrew the queue, which on the
reference board means the result chunks that end a long trial.

**A non-zero `overruns` means the reported timings were taken on a clock that
skipped.** It is counted rather than absorbed for exactly that reason: a board
quietly missing scans looks identical to a board that is fine, and the
difference is a response window measured wrongly. A bridge should surface it.

### 4.6 `pin_map`

```json
{
  "msg_type": "pin_map",
  "message_id": 31,
  "in_reply_to": 9,
  "dir": "in",
  "n": 8,
  "pins": ["D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9"],
  "crc": "...."
}
```

The answer to §3.7. `pins[i]` is what is written on the board beside line `i` of
that direction — silkscreen, not an Arduino pin number: `"A0"` is pin 14 to the
core and `A0` to the person holding the wire, and only one of those is any use
on a bench.

`n` is this board's line count in that direction and is the length of `pins`. It
matches `hello_ack`'s `n_input_lines` / `n_output_lines`; a label table in the
firmware may be longer, and what is answered is what the board actually has,
because that is what a host is allowed to address.

**Input line _n_ and output line _n_ are different pins.** They are two
independent numberings over two disjoint sets of pins, which is why `dir` is
echoed back: a reply that did not say which direction it described could be
filed under the wrong one, and that is a lever's number driving a valve.

A label is free text and carries no structure — a board with screw terminals may
answer `"TB1-3"`, and a host build answers `"sim0"` because it has no pins and
should not pretend to. What a host may rely on is only that the label is stable
for a given firmware build, and that it names the same physical thing the line
number does.

**What this does not prove.** That the wire is actually in the hole the label
names. Nothing in software can: there is no read-back path from a pin. It closes
the gap between the firmware's table and the host's belief about it, which is
the gap that used to be closed by copying; the gap to the soldering iron is
closed by watching a level change when somebody presses the lever.

---

## 5. Errors

`error` carries a machine-readable `code`, a human `message`, and a `context`
naming the specific thing that failed. The bridge switches on `code` and shows
the other two.

| `code`           |                                                                                                                                       |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `bad_crc`        | Line CRC mismatch. Nothing was acted on                                                                                               |
| `too_long`       | Line exceeded `max_line`; discarded to the next newline                                                                               |
| `bad_json`       | Not a parsable object, or deeper than 4                                                                                               |
| `unknown_type`   | Unrecognised `msg_type`                                                                                                               |
| `bad_proto`      | `hello` named a protocol version this firmware does not speak                                                                         |
| `not_ready`      | The command is legal but not in this state — `start` when not armed, `graph_state` before `graph_begin`                               |
| `bad_order`      | A graph message arrived out of the order §3.2 requires                                                                                |
| `bad_index`      | An `i` did not match the count already accepted, or an index named something that does not exist                                      |
| `too_many`       | A capacity was exceeded. `context` names **which one**, so the answer is "raise `max_transitions`", not "make the graph smaller"      |
| `bad_graph`      | The assembled graph failed validation. `context` names the fault                                                                      |
| `graph_mismatch` | `configure` named a `set_version` the device does not hold                                                                            |
| `unknown_trial`  | A `start` or `cancel` for a `trial_id` that is not the armed one                                                                      |
| `busy`           | A trial is in flight and the command is not legal during one                                                                          |
| `no_pin_map`     | `pins` was asked of a build that does not name its pins. The host keeps whatever it assumed, and knows that it assumed it             |
| `bad_field`      | A field was present, parsable, and not one of the values it is allowed to be — `pins` with a `dir` that is neither `"in"` nor `"out"` |
| `internal`       | A bug. Should never appear; if it does, it is one                                                                                     |

**Every refusal names what to change.** An error whose `context` is empty is a
defect in the firmware, not a terse style.

---

## 6. What is deliberately not here

**No trial type, no paradigm, no acceptance.** The device receives a graph,
timings and a reward duration. It never learns whether a trial was a go trial or
a catch trial, and it never decides whether an outcome was _accepted_ — that is
triald's, through the bridge. This is what keeps the firmware stable while
paradigms change, and it is the single most load-bearing omission in this
document.

**No names.** States, lines and distributions are indices on this wire. The
bridge holds the graph and resolves names host-side. On-device name storage is
therefore optional rather than mandatory, which is worth several hundred bytes on
a 32 KB part.

**No outcome vocabulary.** `terminal` and `outcome` are integers the device
copies through without interpreting. They happen to be triald's `.tdr` codes, and
the firmware is deliberately built so that it would not notice if they were not —
a terminal state reports an opaque code and the trial layer above assigns it a
meaning.

**No floating point**, anywhere, in any value that crosses this wire.

**No clock synchronisation.** The device timestamps in its own microseconds and
says so; correlating those with the host's clock is the bridge's problem, and
solving it here would mean pretending to a precision USB CDC cannot deliver.
