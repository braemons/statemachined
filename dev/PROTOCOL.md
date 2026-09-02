# fsmd — the wire protocol

> **Status:** specification. The codec is milestone M2 in [`PLAN.md`](PLAN.md);
> the firmware side of it is being written against this document, not the other
> way round.

The link between the **bridge** (a host process) and the **device** (firmware on
a microcontroller). USB CDC, newline-delimited JSON, a sequence number and a
CRC, as [`PLAN.md`](PLAN.md) specifies.

Everything above the framing is a consequence of two constraints that are worth
stating before the tables, because most of the odd-looking decisions below come
from one of them:

- **The device has 32 KB of SRAM.** No message may require the device to hold a
  document larger than a few hundred bytes. That is why both the graph upload
  and the trial result are *chunked*, and why the path comes back as arrays
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
{"t":"ping","seq":41,"crc":"C083"}\n
```

**Rules**

| | |
|---|---|
| Encoding | ASCII. A byte ≥ 0x80 anywhere in a line is a framing error. Non-ASCII text belongs in `log`, escaped as `\uXXXX` |
| Line length | At most `max_line` bytes including the `\n`. The device reports its own limit in `hello_ack`; the reference board's is **512** |
| Object depth | At most 4. A conforming message never needs more |
| Unknown members | **Ignored**, on both sides. This is how the protocol gains fields without a version bump |
| Unknown `t` | Answered with `error` / `unknown_type`. Never silently dropped |
| Member order | Free, **except** `crc`, which is always last |

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
{"t":"ping","seq":41,"crc":"C083"}
└ covered by the CRC ┘└ not covered ┘
```

`crc` is required to be the final member precisely so that the receiver can find
it without parsing: scan **backwards** from the `}` for `,"crc":"`, CRC
everything before it, compare. That costs one pass and no buffer, which matters
on the device and costs the host nothing.

A message whose CRC does not match is answered with `error` / `bad_crc` naming
the `seq` if one could be read, and is otherwise dropped. **It is never acted
on**, not even partially.

A CRC is not security and is not claimed to be. It catches the failure that
actually happens on a USB CDC link — a truncated or spliced line after a
re-enumeration — early enough that a corrupt graph is refused instead of run.

### 1.2 `seq`

An unsigned 16-bit counter, **independent per direction**, incremented by one
for every line sent and wrapping through zero. It exists for link-level retry
and for nothing else; trial attribution is `trial_id`'s job, and the two are
never conflated.

- Every device reply to a host command carries `req` — the `seq` of the command
  it answers — in addition to its own `seq`.
- Unsolicited device messages (`event`, `log`, and `result_*`) carry no `req`.
- **Every host command gets exactly one reply**, including each message of a
  graph upload. That is what makes a retry decidable: the bridge resends when it
  did not get one, and needs no rule per message type.

- A `seq` equal to **the one the device last answered** makes it repeat that
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
- A gap in `seq` is **not** an error. USB CDC does not lose bytes in the middle
  of a session, and treating a gap as a fault would turn a cosmetic problem into
  a dropped trial. Gaps are reported in `state` for diagnosis.

---

## 2. Types and units

| Notation | Meaning |
|---|---|
| `u8` `u16` `u32` | JSON number, unsigned, within the stated width |
| `i8` `i32` | JSON number, signed |
| `hex64` | **String** of 1–16 hex digits. Used for the session seed, because a 64-bit integer is not representable in a JSON number — a `double` silently loses the low bits, and a seed that silently changes is a reproducibility bug that nobody would find |
| `mask` | `u32`, one bit per line, bit *n* is line *n* |
| `ms` | Milliseconds, `i32`. Every duration a graph declares |
| `us` | Microseconds, `u32`, the device clock. **Wraps every ~71 minutes**; differences are correct across the wrap, absolute values are not comparable between trials |

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
{"t":"hello","seq":0,"proto":1,"seed":"0123456789ABCDEF","crc":"...."}
```

| Field | Type | |
|---|---|---|
| `proto` | `u16` | Protocol version. **1** |
| `seed` | `hex64` | The session seed. Every per-trial stream is derived from it and the `trial_id`, so a session replays exactly from this one number |

Answered with `hello_ack`, or `error` / `bad_proto`.

### 3.2 The graph upload

Six message types, in this order:

```
graph_begin
  graph_dist        × n_distributions
  graph_state       × n_states, in index order
    graph_transition  × this state's transitions
    graph_action      × this state's entry and exit actions
graph_end
```

**`graph_transition` and `graph_action` attach to the most recently declared
`graph_state`.** That is not a convenience: the device stores transitions and
actions as one flat pool per kind, and a state refers to its own as a
`(first, count)` slice of it. A slice is contiguous by construction only if
everything belonging to a state arrives together — so the ordering rule on the
wire *is* the memory invariant, and a violation is refused rather than producing
a state that owns somebody else's transitions.

The upload is staged. **The committed graph is untouched until `graph_end`
succeeds**, so a failed or abandoned upload leaves the device running the
paradigm it was already running.

#### `graph_begin`

```json
{"t":"graph_begin","seq":1,"graph_version":7,"n_states":4,"entry":0,
 "invert":0,"enable":4294967295,"safe":0,"debounce_ms":[0,2,2,0],"crc":"...."}
```

| Field | Type | |
|---|---|---|
| `graph_version` | `u16` | The host's identifier for this graph. Echoed in `armed` and checked by `configure` |
| `n_states` | `u8` | Declared up front so an oversize graph is refused before the first state is sent, not after the last |
| `entry` | `u8` | State index the machine starts in |
| `invert` | `mask` | Lines read active-low. Opto-isolated inputs routinely are |
| `enable` | `mask` | Lines that participate at all. Default all ones |
| `safe` | `mask` | Output levels on watchdog timeout, reset, link loss or a refused graph. Per line, because "off" is not always "low" |
| `debounce_ms` | array of `u16` | Optional. Per input line, index = line. May be shorter than the line count; missing entries are 0 |

#### `graph_dist`

One entry of the shared distribution pool. Timeouts and holds refer to it by
index, so a graph reuses one distribution everywhere it means the same thing.

```json
{"t":"graph_dist","seq":2,"i":0,"kind":"uniform","a":300,"b":700,"crc":"...."}
```

| `kind` | `a` | `b` | `c` | `opts` / `weights` |
|---|---|---|---|---|
| `"fixed"` | the value, ms | — | — | — |
| `"uniform"` | min ms | max ms | — | — |
| `"exponential"` | min ms | max ms | mean ms | — |
| `"choice"` | — | — | — | `opts`: array of ms. `weights`: optional array of `u32`, same length |

`i` must equal the number of distributions already accepted. Stating it
explicitly rather than implying it from arrival order costs four bytes and turns
a dropped message from a silently mis-indexed graph into a refusal.

#### `graph_state`

```json
{"t":"graph_state","seq":6,"i":1,"terminal":null,
 "timeout":{"dist":0,"target":2},"crc":"...."}
```

| Field | Type | |
|---|---|---|
| `i` | `u8` | State index. Must equal the number of states already accepted |
| `terminal` | `i8` or `null` | The outcome code this state reports, or `null` for a non-terminal state. The device treats it as opaque — see §6 |
| `timeout` | object or `null` | `dist` indexes the distribution pool; `target` is the state entered when it expires |

#### `graph_transition`

```json
{"t":"graph_transition","seq":7,"all":3,"any":0,"none":8,
 "target":2,"hold":1,"level":false,"crc":"...."}
```

| Field | Type | |
|---|---|---|
| `all` | `mask` | Every one of these lines must be high |
| `any` | `mask` | At least one must be high. `0` means "don't care" |
| `none` | `mask` | None of these may be high |
| `target` | `u8` | State entered when the predicate fires |
| `hold` | `u8` or `null` | Distribution index for how long the predicate must stay true before it fires. `null` for none |
| `level` | bool | `false` (default): the predicate fires on its own **rising edge**, so a transition already true on entry does not fire until the predicate goes false and true again. `true`: it fires immediately on entry if already true |

Declaration order resolves a tie: the first transition of a state whose
predicate holds is the one that fires. Bpod does the same, and it is the only
tie-break an experimenter can reason about from reading the graph.

#### `graph_action`

```json
{"t":"graph_action","seq":8,"on":"entry","line":2,"kind":"pulse","ms":50,"crc":"...."}
```

| Field | Type | |
|---|---|---|
| `on` | `"entry"` or `"exit"` | When it runs |
| `line` | `u8` | Output line. Must be below the device's `n_output_lines` |
| `kind` | `"high"` `"low"` `"toggle"` `"pulse"` | |
| `ms` | `u16` | `pulse` only: how long it stays high. Ignored otherwise |

**All of a state's `entry` actions must arrive before its first `exit` action.**
The two are separate slices of the same pool and each has to be contiguous, so
interleaving them would silently give one slice the other's members. Refused as
`bad_order`.

**A terminal state's `entry` actions do run**, and they are how a reward is
written: `pulse` the valve line on entering `Hit`. The alternative — hanging it
off the exit of whichever state happened to precede the terminal one — spreads a
single intention across every route into it.

What the device cannot do is lower them, since nothing exits a terminal state.
A `pulse` falls on the device's own timer. Anything set `high` there **stays
high until the next trial starts or a fail-safe runs**, which is deliberate:
prefer `pulse` for anything that must come down on its own.

**Every line a state drives high is driven low again when the state is left**,
by the device, whatever the exit cause and whether or not the graph said so. An
exit action is for what the graph wants *in addition*; a valve left open because
a graph forgot one is not a failure mode this protocol admits.

#### `graph_end`

```json
{"t":"graph_end","seq":40,"n_transitions":6,"n_output_actions":5,
 "checksum":"<crc16>","crc":"...."}
```

`checksum` is CRC-16/CCITT-FALSE accumulated over the **CRC-covered bytes of
every graph message since `graph_begin`, in arrival order, `graph_begin`
included and `graph_end` excluded**. It is not the same thing as the per-line
`crc`: that one catches a corrupt line, this one catches a *missing* one.

`n_transitions` and `n_output_actions` are the host's counts, checked against the
device's. A mismatch is refused, naming both.

On success the assembled graph is validated — every index in range, every slice
inside its pool, no output line the board does not have, a terminal state
reachable from the entry state, and no unreachable state — and then committed.
Answered with `graph_ok` or `error` / `bad_graph`, whose `context` names what
failed.

### 3.3 `configure`

The per-trial message. Arms the device for exactly one trial.

```json
{"t":"configure","seq":41,"trial_id":193,"graph_version":7,"cap_ms":30000,
 "start":"serial","patch":[{"i":0,"a":250,"b":900}],"crc":"...."}
```

| Field | Type | |
|---|---|---|
| `trial_id` | `u32` | The host's identity for this trial. Appears in `armed` and `result`, and a `cancel` for any other id is refused |
| `graph_version` | `u16` | Must match the committed graph. A graph edit that did not land would otherwise leave the device confidently running the old paradigm |
| `cap_ms` | `i32` | Wall-clock cap on the whole trial. `0` or absent means the device default. Validation cannot tell a 10 s foreperiod from a hang, so this stays regardless of the graph |
| `start` | `"serial"` `"line"` `"both"` | What may start the trial once armed |
| `patch` | array | Optional per-trial overrides of distribution parameters, by pool index. Only `a`, `b`, `c` may be patched; `kind` may not. Reverted when the trial ends |

`patch` is why the graph does not have to be re-uploaded when only the timings
change, which is the common case. It cannot change the *shape* of anything —
that would be a different graph, and a different `graph_version`.

Answered with `armed`, or `error`.

### 3.4 `start`, `cancel`, `ping`, `state`

```json
{"t":"start","seq":42,"trial_id":193,"crc":"...."}
{"t":"cancel","seq":43,"trial_id":193,"reason":"host","crc":"...."}
{"t":"ping","seq":44,"crc":"...."}
{"t":"state","seq":45,"crc":"...."}
```

`start` is refused unless the device is armed for that `trial_id` and the
configured `start` source admits serial. **No trial runs that the device was not
confirmed configured for.**

`cancel` takes a `reason` of `"host"`, `"link_lost"`, `"abort_line"` or
`"trial_timeout"`; only `"host"` is legal from the host, the others being the
device's own. A cancel for an unknown `trial_id`, or with no trial in flight, is
**refused rather than acked** — the bridge learns that nothing was cancelled.

A cancel arriving after the machine has already reached a terminal state gets the
**real outcome** back, not a fabricated `CANCELLED`. The first terminal decision
wins and the device reports what actually happened. The bridge must cope with
asking to cancel and being told `HIT`; the alternative is a record claiming a
trial was cancelled when the animal had already responded.

`ping` arms the link-loss watchdog. `state` asks for `state_report` and is for
inspection only — it is never in a trial's critical path.

---

## 4. Device → host

### 4.1 `hello_ack`

```json
{"t":"hello_ack","seq":0,"req":0,"proto":1,"board":"uno_r4_minima",
 "fw":"0.1.0","n_input_lines":8,"n_output_lines":8,"scan_hz":10000,
 "has_graph":true,"graph_version":7,
 "caps":{"max_line":512,"max_states":32,"max_transitions":64,
         "max_output_actions":64,"max_distributions":32,
         "max_choice_options":32,"max_path":64},"crc":"...."}
```

`scan_hz` is **measured at boot, not declared**, so the host knows the timing
resolution it is actually getting rather than the one the design hoped for.

`caps` holds the device's compile-time capacities. The bridge checks a graph
against them before uploading, which turns "refused at `graph_end`" into
"refused before the first byte" — a better error at no cost.

They are **nested rather than flat**, and that is a memory decision rather than a
stylistic one: a receiver's per-message member limit is what bounds how much
stack a parse costs, and flattening these would push the largest message in the
protocol past a limit that every other parse would then pay for.

`has_graph` and `graph_version` say whether a graph survived the reconnect, so a
bridge that dropped its link knows whether it has to re-upload.

### 4.2 `armed`

```json
{"t":"armed","seq":1,"req":41,"trial_id":193,"graph_version":7,"crc":"...."}
```

Both fields, always. This is the confirmation that `start` requires and it is
not skippable.

### 4.3 The result

Chunked, for the same reason the upload is: a 64-entry path does not fit in a
512-byte line, and buffering one that did would cost the device a kilobyte it
does not have.

```
result_begin
  result_path  × ⌈path_len / batch⌉
result_end
```

```json
{"t":"result_begin","seq":9,"trial_id":193,"outcome":1,"cancel_reason":0,
 "total_us":1483200,"path_len":5,"truncated":false,"crc":"...."}

{"t":"result_path","seq":10,"trial_id":193,"from":0,
 "p":[[0,"timeout",255,500,0,500120],[1,"transition",2,0,500120,183044]],"crc":"...."}

{"t":"result_end","seq":12,"trial_id":193,"checksum":"<crc16>","crc":"...."}
```

Each entry of `p` is a fixed six-element array, **not** an object:

```
[state_index, exit_cause, transition_index, drawn_ms, entered_us, duration_us]
```

| Position | Type | |
|---|---|---|
| 0 | `u8` | Which state. An **index**, not a name — the bridge holds the graph and resolves names host-side, which is part of what keeps the device inside 32 KB |
| 1 | string | `"timeout"` `"transition"` `"cancel"` `"terminal"` |
| 2 | `u8` | Which transition fired, or `255` for an exit that was not one |
| 3 | `i32` | The **realised** duration of the draw, in ms. Reported so a random timing is evidence in the record and not merely reproducible from the seed |
| 4 | `u32` | Entry timestamp, device clock |
| 5 | `u32` | Measured duration. This is what actually happened; position 3 is what was asked for |

Objects would be clearer to read and roughly twice the bytes. The array form is
documented once, here, and decoded once, in the bridge.

`truncated` is set when the run visited more states than `max_path` holds. **A
graph may loop, and a long trial degrades to a truncated path rather than to a
corrupt one** — the record is a ring buffer with an overflow flag, and the flag
is on the wire so the host never mistakes a truncated path for a complete one.

`result_end`'s `checksum` accumulates over `result_begin` and every
`result_path`, exactly as `graph_end`'s does, and catches a dropped chunk.

### 4.4 `event`, `error`, `log`, `pong`, `state_report`

```json
{"t":"event","seq":13,"us":1483200,"word":6,"crc":"...."}
{"t":"error","seq":14,"req":41,"code":"bad_graph","message":"...","context":"...","crc":"...."}
{"t":"log","seq":15,"level":"warn","message":"...","crc":"...."}
{"t":"pong","seq":16,"req":44,"up_us":90210000,"crc":"...."}
```

`event` reports the conditioned input word on change. **Off by default and never
in a trial's critical path**: it is a monitoring aid, and a link that cannot keep
up drops events rather than delaying a scan.

`log` is free text, rate-limited, and never load-bearing. Nothing in the bridge
may parse it.

`state_report` answers `state` with the current state index, the live input word,
the live output word, uptime, the committed `graph_version`, and the counts of
CRC failures and `seq` gaps seen. Diagnosis, not control.

---

## 5. Errors

`error` carries a machine-readable `code`, a human `message`, and a `context`
naming the specific thing that failed. The bridge switches on `code` and shows
the other two.

| `code` | |
|---|---|
| `bad_crc` | Line CRC mismatch. Nothing was acted on |
| `too_long` | Line exceeded `max_line`; discarded to the next newline |
| `bad_json` | Not a parsable object, or deeper than 4 |
| `unknown_type` | Unrecognised `t` |
| `bad_proto` | `hello` named a protocol version this firmware does not speak |
| `not_ready` | The command is legal but not in this state — `start` when not armed, `graph_state` before `graph_begin` |
| `bad_order` | A graph message arrived out of the order §3.2 requires |
| `bad_index` | An `i` did not match the count already accepted, or an index named something that does not exist |
| `too_many` | A capacity was exceeded. `context` names **which one**, so the answer is "raise `max_transitions`", not "make the graph smaller" |
| `bad_graph` | The assembled graph failed validation. `context` names the fault |
| `graph_mismatch` | `configure` named a `graph_version` the device does not hold |
| `unknown_trial` | A `start` or `cancel` for a `trial_id` that is not the armed one |
| `busy` | A trial is in flight and the command is not legal during one |
| `internal` | A bug. Should never appear; if it does, it is one |

**Every refusal names what to change.** An error whose `context` is empty is a
defect in the firmware, not a terse style.

---

## 6. What is deliberately not here

**No trial type, no paradigm, no acceptance.** The device receives a graph,
timings and a reward duration. It never learns whether a trial was a go trial or
a catch trial, and it never decides whether an outcome was *accepted* — that is
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
