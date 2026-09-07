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
> write down afterwards, is [`BRINGUP.md`](BRINGUP.md). This section is the
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

On the board, with `daemon/` driving it over USB CDC.

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

`dev/DAEMON.md` §3.2 uploads every graph a session uses once and then switches
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

**Still not measured:** input-to-output latency and the step dwell as seen by a
scope on D2 and D10. The figure above is the device's own clock reporting on
itself, which is a different claim from a probe on a pin.

**What *is* verified without a board:** the pin map above, the port-register
reads and writes, the timer ISR, and a whole session over a real UART, all under
Renode in CI. See `emulation/README.md` — and note that it proves the HAL
correct and says nothing whatever about how long a scan takes.

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
