# statemachined — hardware

Per-board pinouts, wiring, and the numbers actually measured on each one.

The line map on this page is a **wire contract in the same sense the protocol
is**. A graph names `line: 3`, not a pin; the bridge resolves names to line
numbers; and the board resolves line numbers to pins here. Renumbering a line
silently changes what every existing graph does, so it is a breaking change and
belongs in a release note.

---

## Uno R4 Minima — the reference target

Renesas RA4M1 (Cortex-M4 at 48 MHz), 32 KB SRAM, 256 KB flash, native USB CDC.
The only board we have, and the one that sets the design.

### Line map

Eight in, eight out. The board has more usable pins than that, but D0/D1 are the
UART and D13 carries the on-board LED; a line map that quietly includes either
is one that surprises somebody at 2 a.m.

| input line | Pin | | output line | Pin |
|---|---|---|---|---|
| 0 | D2 | | 0 | D10 |
| 1 | D3 | | 1 | D11 |
| 2 | D4 | | 2 | D12 |
| 3 | D5 | | 3 | A0 |
| 4 | D6 | | 4 | A1 |
| 5 | D7 | | 5 | A2 |
| 6 | D8 | | 6 | A3 |
| 7 | D9 | | 7 | A4 |

Defined in `firmware/hal/renesas_ra4m1.cpp`, as two arrays indexed by line
number. The Arduino pin number is turned into a port and a bit with the core's
own `digitalPinToBspPin()` rather than a hand-written table: a hand-copied pin
map is a silent wrong-valve bug and the core already knows the answer.

**Three things about this table are load-bearing, and none of them is
configurable.**

**Which pin is an input and which is an output is fixed when the firmware is
compiled.** `init()` calls `pinMode()` over these two arrays and nothing
afterwards changes a direction: the `wiring` command (`PROTOCOL.md` §3.5)
carries invert, enable, debounce and output safe levels, and no direction field
exists in it. So a rig that needs D9 to drive a valve does not edit a config
file — it edits `kInputPins`/`kOutputPins` and reflashes. That is deliberate. A
direction that could be changed over the wire is a valve line that can be turned
into an input by a bad config, and a board comes up long before any config
reaches it.

**Input line *n* and output line *n* are different pins.** They are two
independent numberings over two disjoint sets of pins, because the protocol
carries two separate words. Input line 3 is D5; output line 3 is A0. There is no
line 3 in the sense of "one pin".

**The line number is what a graph means — and the board is asked what the pin
labels are, rather than told.** `line_index` in a state-machine config
(`/var/lib/braemons/statemachined/configs/`) is a bit position in those words
and is the only part that reaches the device. `pin_label` beside it names the pin, and it
used to be free text checked against nothing: writing `D9` next to input line 1
did not move it — line 1 is D3 because `kInputPins[1]` is 3 — it only put a
wrong label on the web UI for the next person.

It is checked now. The `pins` command (`PROTOCOL.md` §3.6) answers with this
table, out of the firmware that holds it, so:

- a line may name **only** a pin — `pin = "D6"` — and the daemon resolves it to
  line 4 by asking the board;
- a line naming both has them **checked**, and a pair that disagree stops the
  daemon connecting rather than being pushed;
- a line naming a pin this board does not have — or naming an output's pin as an
  input — is refused, with the board's actual pins in the message.

**The first of those three is the form to write, and it is what the files in
this repository now use.** `graphs/uno-r4-minima-lines.json` and the bench
conffile name a pin per line and no bit position at all. A `line_index` beside
a pin is checked and adds nothing: it is the half of the pair that is written
nowhere on the hardware, so nobody at the bench can confirm it — and it is the
half that silently stops being true when this table is reordered. A map written
in pins follows a reflash; one written in indices goes on naming the old holes.

`make bringup ARGS="--hello pins"` prints exactly what the board answers.

What none of that proves is that the wire is in the hole the label names. There
is no read-back path from a pin, so the Lines panel showing each line's live
level is still the only verification of *that*: press the lever, watch which dot
lights.

The graph pools are sized for 32 lines on every board so the data structures do
not change shape per target. What this board can physically drive is the eight
above, reported in `hello_ack` as `n_input_lines` / `n_output_lines`, and the
bridge checks a graph against them before uploading a byte of it.


### Demo mode — the bring-up wiring

> The step-by-step procedure, including what to check at each stage and what to
> write down afterwards, is [`bringup.md`](bringup.md). This section is the
> wiring it refers to.

The board holds no graph until one is uploaded, so a bench board wired up and
powered on does nothing but blink -- see BRINGUP.md §3, and §7a for giving it a
graph to run out of its own storage. This wiring is what the shipped examples in
`graphs/` and the line map in `configs/uno-r4-minima-bench.config.json` expect.

| What | statemachined line | Pin | Wire it as |
|---|---|---|---|
| Start switch | input 0 | **D2** | switch to **5 V**, plus a **10 kΩ pull-down to GND** |
| Abort switch | input 1 | **D3** | the same |
| Ready lamp | output 0 | **D10** | LED anode to pin, cathode through **220-330 Ω** to GND |
| Cue lamp | output 1 | **D11** | the same |
| Error lamp | output 2 | **D12** | the same |
| Reward valve | output 3 | **A0** | an LED on the bench; on a rig, the driver |
| Alive heartbeat | *not a line* | **D13** (on-board LED) | nothing — it is the LED already on the board |

**The pull-downs are not optional.** `hal::init()` sets inputs to `INPUT`, not
`INPUT_PULLUP` — deliberately, since a rig's TTL sources drive both ways and a
pull-up fights them. A switch with nothing else on the pin therefore leaves it
floating when open, and a floating input picks up enough noise to start and
abort trials on its own. If you have no resistors to hand, the degenerate test
is a jumper wire from 5 V touched to D2, which is bouncy but unambiguous.

What you should see, with nothing attached at all: **D13 blinks** briefly once a
second. That alone says the board booted, the `FspTimer` ISR is running and the
scan loop is turning, which are the three things that fail first. Nothing else
moves, because nothing has given the board anything to run.

With `graphs/state-walk.json` uploaded and the board told to arm its own trials
(BRINGUP.md §7a): the ready lamp on D10 is lit and stays lit. Press the start
switch and one lamp walks D10 → D11 → D12 and round again, **500 ms each**, then
the trial ends as `Hit` and D10 comes back on; 1.5 s later it goes again, which
is the dwell that graph's terminal state declares.

---

## The loopback harness

A second wiring, for testing rather than for a bench: **eight jumper wires,
output line _n_ to input line _(n + 4) mod 8_**, so the board drives its own
inputs.

| Wire | Output line | From | | To | Input line |
|---|---|---|---|---|---|
| 1 | 0 | **D10** | → | **D6** | 4 |
| 2 | 1 | **D11** | → | **D7** | 5 |
| 3 | 2 | **D12** | → | **D8** | 6 |
| 4 | 3 | **A0** | → | **D9** | 7 |
| 5 | 4 | **A1** | → | **D2** | 0 |
| 6 | 5 | **A2** | → | **D3** | 1 |
| 7 | 6 | **A3** | → | **D4** | 2 |
| 8 | 7 | **A4** | → | **D5** | 3 |

No components; the inputs are high-impedance and the outputs drive both ways.

**What it is for.** A graph's entry action raises an output, and one scan later
that arrives as an input the same graph's transitions can wait on. Three things
become testable that otherwise are not:

- **Predicates on real silicon.** The chain pin → `InputConditioner` →
  `Transition::matches()` → a transition firing is exercised nowhere else. Every
  other trial in this repository ends on a timeout.
- **Simultaneity.** Two lines released and pressed again inside one millisecond
  is not something a person can do to a pair of switches, and it is exactly what
  a rising-edge rule over a two-line predicate has to be tested against.
- **Response latency.** The measured duration of a state that raises a line and
  waits for it *is* the board's pin-to-transition time, which is the one timing
  figure the device can honestly measure about itself.

**Why the shift, rather than output _n_ to input _n_.** A straight-through
harness cannot distinguish a correct board from one whose reported input word is
secretly the output word: raise output 0, see bit 0 set, pass. Under the shift
each output has a unique and non-obvious expected input bit, so that failure —
and any rotation or off-by-one in either table above — fails rather than passing
for the wrong reason. It also covers all sixteen lines; the three-wire harness
this replaced left thirteen pins never once proven to be the pin the table
claims.

**It is not compatible with the bench switches.** Wires 5 and 6 land on D2 and
D3, which the bench wiring above drives from a contact to 5 V, and an output
pulling low against a closed switch is a short. Either take the switches off, or
put **1 kΩ in series** in those two wires. The output pins may be shared with
the bench lamps freely.

**In software, with no board.** The host build implements the same rule —
`hal::set_native_loopback(width, shift)`, switched on with
`STATEMACHINED_LOOPBACK=8` — so a suite that drives transitions from predicates
runs unchanged with a board and without one. That is what
`python/tests/runs/` relies on: the far end is a fixture, and the same session
runs against silicon and against the host build. The software half reproduces
*which line a level arrives on and that it arrives a scan later*; it reproduces
nothing about timing, and no timing assertion is made against it.

Holding the start switch down does not re-trigger: a transition fires on its
predicate's *rising edge*, so the switch has to be released and pressed again.
That is the same rule that stops a lever the animal is already holding from
ending a trial the instant it begins.

The moment a host sends `hello`, it **takes the rig**: a board arming its own
trials stops, the run in flight is cancelled through the ordinary exit path, and
every line goes to its safe level. A serial *monitor* opening the port is not
enough — it is the greeting that hands over, not the connection.

### Electrical

**Inputs are configured `INPUT`, not `INPUT_PULLUP`.** A rig's TTL sources drive
both ways, and a pull-up on a line an opto-isolator is sinking is a line that
never reads low. Anything that needs a pull-up gets a resistor, which is visible
on the bench.

**3.3 V logic, 5 V tolerant on the digital pins.** The RA4M1 runs at 3.3 V. TTL
sources at 5 V are fine on inputs; a 5 V input expecting a 5 V high from an
output needs a level shifter, because 3.3 V is marginal against a 5 V TTL
threshold.

**Nothing on these pins drives a valve directly.** Output current is a few mA.
A solenoid takes a driver board, and the driver's own enable-low or enable-high
convention is what `safe` in `graph_begin` exists for — "off" is not always
"low".

### How the pins are actually driven

Inputs: one `PCNTR2` read per *port*, not one per line, then bits are gathered
into the word. The Renesas core's `digitalRead()` costs 1–2 µs, so eight of them
would be a fifth of the scan budget before anything had been decided.

Outputs: `PCNTR3`, which is set-and-reset in one write-only 32-bit register
(`POSR` low, `PORR` high). No read-modify-write, so it is safe against an
interrupt touching another pin on the same port, and it costs one store per port
however many lines moved.

### Measured

Built with the PlatformIO env `uno_r4_minima`, 2026-09-02.

**Static RAM — measured, and it is not what `dev/PLAN.md` estimated.**

| | Bytes | |
|---|---|---|
| `HostLinkSession` (the whole device state) | 8896 | staged graph, live graph, trial runner, buffers, retry cache |
| `InputConditioner` | 208 | |
| USB CDC, tinyusb, FSP, core | ~2800 | not ours, not removable |
| **`.data` + `.bss` + `.noinit`** | **11 928** | what `pio run` reports: 36.4% |
| Framework heap (`BSP_CFG_HEAP_BYTES`) | 8192 | reserved by the variant. **statemachined never allocates** |
| Main stack (`BSP_CFG_STACK_MAIN_BYTES`) | 1024 | declared; the physical gap below it is ~8.3 KB |
| **Committed** | **21 144** | **64.5% of 32 KB** |

Flash: 58 492 B, 22.3% of 256 KB.

Three things follow, and they are the point of measuring rather than estimating:

1. **The plan's ≈ 7.5 kB / ~23% budget was wrong by a factor of about three**,
   and it fits anyway. The estimate costed the graph pools and forgot that the
   device holds *two* graphs — the live one and the one being staged by an
   upload — plus a full-line retry cache, plus the USB stack.
2. **A quarter of the SRAM is a heap nothing uses.** `BSP_CFG_HEAP_BYTES` is
   `0x2000`, fixed in the variant's `bsp_cfg.h`, and the core is what wants it.
   The portable core allocates nothing. Recovering it means patching the
   framework, which is worth doing only if something needs the room.
3. **The declared main stack is 1 KB and a JSON parse is not far off it** —
   `JsonObject` is roughly 536 B and two can be live while a nested object is
   read. It does not overflow, because the linker puts `__StackTop` at the top of
   RAM and the real gap before the heap is ~8.3 KB, but the declared figure is
   not the true headroom and nobody should read it as one.

### Scan rate and link cost — measured 2026-09-03

On the board, with `python/` driving it over USB CDC.

| | |
|---|---|
| `scan_hz` from `hello_ack` | **124 680 Hz** — 12× the 10 kHz timer |
| `scan.overruns` under link load | **3.0 per command** (`ping`), **9.1** (`state`) |
| `scan.worst_gap` | **9** periods |
| `scan.tx_stalls` | 0 |

`scan_hz` is the floor cost of reading and conditioning the pins — about 8 µs,
against a 100 µs period — so the pins are not what limits this board.

**The link is, and by a lot.** Servicing one command costs roughly 3 ms of CPU,
and almost all of it is the vendor USB stack rather than anything here:

| per command, at 10 kHz | µs |
|---|---|
| draining the link (TinyUSB) | 1240 |
| reading the link (TinyUSB) | 754 |
| `tud_task()`, via `link_up()` | 533 |
| `HostLinkSession` — ours | 372 |
| the scan itself | 7 |

That measurement is why the scan runs in the timer ISR rather than in `loop()`:
queued behind those milliseconds it was missing **9.9 periods per command**, and
climbing for as long as a bridge kept talking. The reasoning, and the handoff
that fixes it, are at the top of `firmware/src/main.cpp`.

What is left — 3.0 periods per `ping` — is exactly the window in which the
foreground holds the engine to parse a command and build its reply, and nothing
else. It is bounded by our own code rather than by the USB stack's behaviour,
which is the property worth having.

These numbers are also a test now, rather than only a record: `make test-hardware`
budgets a ping at 8 periods and a `state_report` at 20, against the 3.0 and 9.1
measured here. Reverting the handoff fails it on the first assertion, which is
the point — a measurement written down once is a measurement that quietly stops
being true.

### What `pins` costs — measured 2026-09-04

The board answering which pin each line is (`PROTOCOL.md` §3.6) is the cheapest
thing in this document.

| | rig image |
|---|---|
| Flash | 69 048 B → **69 672 B** (+624 B: two label tables, the handler, the two names) |
| RAM | 13 656 B → **13 664 B** (+8 B: the two pointers `DeviceIdentity` carries) |

The label tables are in flash — `nm` puts both at 0x15300 — because a
`constexpr` table of pointers to string literals is not copied into RAM. What
the 8 B buys is a host that no longer keeps its own copy of this pinout.

### The graph set, over the link — measured 2026-09-04

`docs/developer/daemon.md` §3.2 uploads every graph a session uses once and then switches
by index, and the whole argument for that is the second row of this table.

| | |
|---|---|
| Uploading a set: 2 graphs, 15 states, 11 transitions, 9 actions, 5 distributions | **205 ms** |
| `configure`, switching to a graph already on the board | **27 ms** |
| A trial capped at 8 000 ms, as the device measured it | 8 000 055 µs |

The 205 ms is paid once per session, before the first trial. The 27 ms is what
each inter-trial interval actually pays, and it is the number §3.2 exists to
produce: uploading go/no-go per trial would put the first figure there instead.

The set used 2 of 20 graph slots and 15 of 32 states, which is the capacity
question open question 10 asks about — a real paradigm set is what will settle
it, not this one.

### Trial timing — measured 2026-09-03

A two-state graph uploaded over the link, `wait --(500 ms)--> Hit`, raising line
2 on entry:

| | |
|---|---|
| Declared dwell | 500 000 µs |
| Measured (`duration_us` in `result_path`) | **500 078 µs**, +78 µs |

78 µs is less than one scan period, which is the resolution the design claims
and the first evidence from a board that it holds. The same run confirms the
entry action reached the pin — `io.out` read 4 mid-trial — and that the result
chunks, which the scan ISR pushes while the foreground drains them, arrive whole
and in order with their rolling checksum intact.

**Still not measured:** the step dwell as seen by a scope on D2 and D10. The
figure above is the device's own clock reporting on itself, which is a different
claim from a probe on a pin. Input-to-output latency *has* since been measured —
by the board against itself, through the loopback harness: 228 µs mid-trial,
with a further ~1.1 ms on the trial's first state only. See "Response latency
and duration accuracy" below.

**What *is* verified without a board:** the pin map above, the port-register
reads and writes, the timer ISR, and a whole session over a real UART, all under
Renode in CI. See `emulation/README.md` — and note that it proves the HAL
correct and says nothing whatever about how long a scan takes.

### Response latency and duration accuracy — measured 2026-09-09

Through the loopback harness, on an Uno R4 Minima, asserted from now on by
`python/tests/hardware/test_timing_accuracy.py`.

**Duration accuracy.** A fixed dwell, ten trials at each of five scales:

| Drawn | Error, min … max | Mean |
|---|---|---|
| 20 ms | +14 … +88 µs | +63 µs |
| 50 ms | +13 … +81 µs | +46 µs |
| 100 ms | +8 … +73 µs | +31 µs |
| 500 ms | +7 … +85 µs | +39 µs |
| 1000 ms | +19 … +87 µs | +66 µs |

The error is **flat across fifty times the duration**, which is the part that
matters: it is the fixed cost of entering and leaving a state, not a clock
running fast. A rate error would have grown with the dwell, and at 1000 ms it is
66 µs — 66 ppm — so the device's clock is good to well under a part in ten
thousand over a trial. Jitter on a repeated 100 ms dwell is sd ≈ 25 µs.

**Response latency — decomposed and then fixed, 2026-09-09.** A state that
raises one output and waits for the input its jumper drives began as a single
alarming figure: median 1 196 µs, bimodal at ≈1 150 and ≈1 950 µs, 12 to 20 scan
periods for a chain that touches nothing but GPIO. It turned out to be three
separate costs stacked on one measurement, and none of them was the pin path.

| | before | after |
|---|---|---|
| Pin → transition, mid-trial | 223 µs | **100 µs** — min = max = median, sd 0 |
| The same wait, entered by `start` | 1 196 µs (bimodal) | 809 µs median, 1 561 max |
| `start` stamp → first scan, no pins at all | 1 117 µs | 812 µs median, 1 562 max |

**First: the `start` command's timestamp.** `entered_us` was stamped from the
timestamp `service_link()` took off the link *before* it raised the
`EngineHold`, while the entry state's outputs waited for the next scan. A graph
whose entry state had no actions and a 0 ms timeout still reported a first visit
of **1 117 µs** (sd 4, unimodal) — no pin, no conditioner, no predicate in it.
Every trial's first state reported about a millisecond it had not spent, and its
entry action reached the pin about a millisecond after the timestamp that said
it had. `on_start` now hands the run to the next `advance_trial()`, so the scan
that stamps the trial is the scan that drives its pins — one call, the shape
autorun's first run always had.

**Second, and the one that mattered for a response window: the visit stream was
being serialised inside the scan ISR.** Building a ~130 byte `visit` line with a
CRC costs about 120 µs on this part. Paying that inside `advance_trial()` — and
on a board `advance_trial()` *is* the timer interrupt — made the scan overrun
its own tick, so the **next** scan landed late and the board's answer to a line
was 223 µs instead of one scan period. Measured both ways on the same board:

| entry actions on the waiting state | 1 | 4 | 8 |
|---|---|---|---|
| visit stream formatted in the ISR | 222 µs | 224 µs | 227 µs |
| visit sink unbound (diagnostic build) | **100 µs** | 100 µs | 100 µs |

Exactly one scan period, sd 0, and insensitive to how much else the entry action
did — which is what says the remaining 100 µs is the period itself and not a
cost hiding inside it. The pin is driven at the end of scan *N* and read at scan
*N+1*; there is nothing else in there.

`ScanHealth` did not show this. It counts periods with no scan in them, and a
scan that runs *long* is not a scan that did not run.

**Why raising the scan rate did not help.** It was the obvious lever and it did
nothing: at 20 kHz the latency was 220 µs, against 223 at 10 kHz, while overruns
per `ping` doubled from 3.4 to 7.9 — so the timer genuinely doubled and the
number did not move. Shortening the period cannot shorten the ISR that is
overrunning it. **The fix is what makes the scan rate a lever at all**: with the
formatting out of the interrupt, the response path is one period, so 20 kHz
would be 50 µs and 50 kHz 20 µs, against a measured scan floor of 8.2 µs.

**The fix.** `advance_trial()` copies the completed `StateVisit` into a 32-deep
ring and returns. `drain_outbound()`, called from `loop()`, does the JSON and the
CRC — **and the result too**: `emit_result()` moved out of the trial loop with
it, since a `result_begin` plus its `result_path` chunks plus a `result_end` is
the largest burst of formatting the device does, and it was on the interrupt for
the same reason the visits were. `advance_trial()` now flags the ended run and
the foreground sends it, after the visits it summarises. Three further things
follow:

- **`ReplySink::send_line()` is no longer reachable from an interrupt at all.**
  It spins when the transmit queue is full, which is correct from the foreground
  and a deadlock from the ISR — the queue only drains through USB work the
  foreground has to run. A graph of eight states with 0 ms timeouts stalled a
  `start` at 10 kHz; a 20 kHz build stopped answering USB and needed a
  double-tap reset; and an intermediate version of this very change, which left
  `emit_result()` calling the visit drain from inside the trial loop, wedged the
  board the same way while every host and integration test passed — because on
  the host `advance_trial()` is an ordinary call and there is no interrupt to
  deadlock. That is the argument for the rule rather than for care: nothing that
  formats a line runs in the trial loop.

- **Draining is paced.** `loop()` sends one visit per pass, interleaved with the
  transmit drain. Emptying a full ring in one pass hands the queue more lines
  than it holds and `send_line()` spins waiting for the wire — 21 such stalls on
  the reference board, against a budget of zero.
- **A full ring drops the newest visit and counts it**, reported as
  `scan.visits_dropped` in `state_report`. Dropping the newest rather than the
  oldest is what keeps the ring single-producer/single-consumer and therefore
  lock-free with an interrupt at one end. The host sees the gap either way, in
  the `seq` field that exists for exactly this. **The result is unaffected**:
  `result_path` is built from the machine's own record, not from the stream, so
  a dropped visit costs a live trace and never the record.

The ring is 32 deep (~640 B) because it has to cover the longest the foreground
can go without draining — one link command, about 2.5 ms, or 25 scans at 10 kHz,
against the worst gap of 22 measured here. It is deliberately *not* sized to
hold a trial: a graph with a loop produces far more visits than it has states,
and the thing that must hold a whole trial is `kMaxPath`.

**What this means for a paradigm.** The response path is one scan period —
100 µs at 10 kHz, min = max = median over 60 trials, and it follows the scan
rate if that is ever raised. A state entered by `start` still pays the link's
overhead in *when it begins* — 812 µs of foreground, median — but no longer in
what it reports: its `entered_us` is the scan's clock and coincides with its own
pins. The remaining 812 µs is `receive()` parsing the command and building the
`started` reply with the engine held, and the note at the top of
`firmware/src/main.cpp` says what shrinking it would take.

### Starting a trial on a line

A trial armed with `start` of `"line"` or `"both"` begins on the rising edge of
`start_line`, in the scan that sees it. See docs/reference/protocol.md §3.4 for the
shape; what matters here is the timing and what it costs.

**It is the cheapest start there is**, and cheaper than the serial one by the
whole of the link's foreground: the edge, the trial's `entered_us` and the pins
its entry action drives are one call in the scan, so the path from pin to first
output is one scan period — the same 100 µs measured above for a mid-trial
response, and for the same reason. There is no equivalent of the serial path's
812 µs, because no foreground work is in the path at all. Telling the host is
the part that waits: the unsolicited `started` is formatted by `loop()`, like
every other line this device sends, and it lands a pass or so after the trial is
already running.

**Not verified on this board.** The eight-wire loopback cannot produce the edge
this needs. A trial is only armed while none is running, and the only thing that
moves an input on this harness is a *running* trial's output — so there is no
sequence of loopback trials that raises a line while the board is waiting for
one. It is covered at the unit tier instead, where the conditioned word is the
test's to write (`tests/core/protocol/test_host_link_session.cpp`, eight cases:
the edge, a line already high at arm time, one edge starting one trial, `"both"`
racing both ways, the three refusals, and disarming on link loss).

Confirming it on silicon needs a signal the board does not generate: a bench
supply, a function generator, or a jumper from a second board's output into the
start line. What to expect — the `started` arrives with `"by":"line"`, its `at_us`
equal to the first visit's `entered_us`, and the entry action's pin up one scan
period after the edge.

### Global timers, and what they cost the scan

A global timer runs beside the state machine: it holds one bit of the input word
high while it runs, optionally drives one real output line alongside, and
outlives the trial that started it. See `docs/reference/protocol.md` §3.2
(`graph_timer`) for the model and `firmware/core/graph/global_timer.h` for what
it takes from VStim and what from Bpod.

**They are what makes a line start testable on this rig.** The eight-wire
loopback cannot produce the edge a `start: "line"` trial waits for, because a
trial is only armed while none is running and the only thing that moves an input
on that harness is a *running* trial's output. A timer breaks the deadlock: a
trial's last state starts one, the trial ends, and the timer raises its line
while the board sits armed. That chain is asserted in
`tests/core/protocol/test_host_link_session.cpp` ("a global timer outlives the
trial that started it, and starts the next one"), which plays the loopback
itself.

**Cost per scan.** Two early-outs keep it off the hot path, and both are rules
this firmware already had:

- Nothing running: `armed_` is zero, and the whole phase pass is one compare.
- Input word unchanged: `holds()` is a pure function of the word, so no trigger
  can have a false→true edge and the whole trigger pass is one compare. That is
  `dev/PLAN.md`'s second rule, the same one that makes scanning predicates
  affordable instead of indexing a Bpod-style matrix.

So the common scan — a rig at rest between responses — costs two compares
regardless of how many timers the set declares. Only a scan on which the word
actually moved pays for the eight predicate evaluations.

**SIMD does not apply here**, and it is worth writing down why so the question
is not reopened. Neither target has a vector unit: the RA4M1 is a Cortex-M4 and
the Teensy 4.1 an M7, and both have the ARM DSP extension (packed 8- and 16-bit
lanes in a 32-bit register) rather than NEON. More to the point, the per-timer
work is a phase switch, a deadline compare and an occasional distribution draw —
branches, not arithmetic throughput. Vectorising it would mean making all eight
timers do every branch's work unconditionally, which is strictly slower than
skipping seven of them. The two early-outs above are the optimisation that was
actually available.

### Timer accuracy

**No PWM.** A timer's output line is driven through the same `OutputUpdate` →
port-register path as every other output on this device: binary high or low, on
the scan. `analogWrite` appears nowhere in the firmware, and no hardware timer
peripheral is involved. What `loops` and `gap` give is a square wave at
millisecond resolution — usable down to a few hundred hertz, which is a valve or
a shutter, not an LED dimmed to 40%. Intensity control would mean a GPT channel
on the RA4M1 and a genuinely different feature; Bpod has it (`OnLevel`/`OffLevel`
take 0–255 on its PWM channels) and this does not.

**One edge.** Every duration is declared in whole milliseconds and served on the
scan, so an edge lands on the first scan at or after its deadline: late by less
than one scan period, and **never early**. At 10 kHz that is 0–100 µs, on top of
the crystal error already measured for state durations (+8 to +88 µs, 66 ppm at
1000 ms — see above; it is the same `micros()`).

**Many edges — the part worth checking.** That per-edge lateness must not
accumulate, and the arithmetic that decides whether it does is one line: the next
phase's deadline is measured from **the deadline just met**, never from the scan
that noticed it. Measuring from the scan folds each edge's lateness into the
schedule and adds it up, which turns a bounded error into a wrong *rate* — a free
running timer slowly losing time, invisible in any test short enough to eyeball.

That was the first implementation here, and it was wrong. `test_global_timers.cpp`
("a free-running timer does not drift") now pins it: against the old arithmetic
the error grows about 90 µs per cycle, monotonically, reaching 4320 µs by the
forty-eighth edge — 47 of its 48 checks fail. VStim avoids the same trap the same
way and says so (`m_NextPulse_HR += m_Periode_HR`).

The tests tick on a **107 µs** grid rather than a 10 kHz one on purpose. 100 µs
divides a millisecond exactly, so a scan-aligned test would place every deadline
on a scan boundary and hide the entire question; 107 is co-prime with 1000, so
each deadline falls between two scans and is noticed late by a different amount,
which is what a real board looks like.

**Falling a whole cycle behind resyncs rather than catching up.** After a long
stall the schedule restarts from now and the missed cycles are dropped, because
emitting a burst of truncated pulses to make up ones nobody saw is worse than
losing them — for a valve it would be a dose nobody ordered.

**Not measured on the board.** The figures above are structural, not timed. The
scan floor here is 8.2 µs against a 100 µs period, so there is room, but nobody
has yet put a set with eight running timers on an Uno R4 and read `worst_gap`
back. That is the measurement to take before relying on eight of them at 20 kHz.

### Flashing

```sh
make upload            # pio run -e uno_r4_minima -t upload
```

From inside the devcontainer this needs the board passed through — see
`BUILD.md`. Double-tap RESET puts the R4 in its bootloader if a sketch has taken
the port over.

---

## Teensy 4.1, ESP32

Not yet. See `dev/PLAN.md`, milestone M6. Nothing in the design needs their
resources; they are there to prove the portable core was worth having.
