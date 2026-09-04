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

| statemachined input | Pin | | statemachined output | Pin |
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

The graph pools are sized for 32 lines on every board so the data structures do
not change shape per target. What this board can physically drive is the eight
above, reported in `hello_ack` as `n_input_lines` / `n_output_lines`, and the
bridge checks a graph against them before uploading a byte of it.


### Demo mode — the bring-up wiring

> The step-by-step procedure, including what to check at each stage and what to
> write down afterwards, is [`BRINGUP.md`](BRINGUP.md). This section is the
> wiring it refers to.

Before any host says `hello`, the board runs a built-in graph so that a bench
board is visibly alive (`firmware/core/demo/demo_graph.cpp`; compile it out with
`-DSTATEMACHINED_DEMO=0`). It uses two inputs and six outputs, and it is the cheapest way
to find out whether your wiring reaches the lines you think it does.

| What | statemachined line | Pin | Wire it as |
|---|---|---|---|
| Start switch | input 0 | **D2** | switch to **5 V**, plus a **10 kΩ pull-down to GND** |
| Abort switch | input 1 | **D3** | the same |
| Step LEDs 1-5 | outputs 0-4 | **D10, D11, D12, A0, A1** | LED anode to pin, cathode through **220-330 Ω** to GND |
| Ready / done lamp | output 7 | **A4** | the same |
| Alive heartbeat | *not a line* | **D13** (on-board LED) | nothing — it is the LED already on the board |

**The pull-downs are not optional.** `hal::init()` sets inputs to `INPUT`, not
`INPUT_PULLUP` — deliberately, since a rig's TTL sources drive both ways and a
pull-up fights them. A switch with nothing else on the pin therefore leaves it
floating when open, and a floating input picks up enough noise to start and
abort trials on its own. If you have no resistors to hand, the degenerate test
is a jumper wire from 5 V touched to D2, which is bouncy but unambiguous.

What you should see, with nothing attached at all: **D13 blinks** briefly once a
second. That alone says the board booted, the `FspTimer` ISR is running and the
scan loop is turning, which are the three things that fail first.

With the LEDs and the start switch wired: the ready lamp on A4 is lit, and stays
lit. Press the start switch and one LED walks D10 → D11 → D12 → A0 → A1, **500 ms
each**, then the trial ends as `Hit` and A4 comes back on. Press the abort switch
mid-walk and it stops immediately, leaving D10 lit as a `Cancelled` lamp. Either
way the next trial arms 1.5 s later.

Holding the start switch down does not re-trigger: a transition fires on its
predicate's *rising edge*, so the switch has to be released and pressed again.
That is the same rule that stops a lever the animal is already holding from
ending a trial the instant it begins.

The moment a host sends `hello`, demo mode ends for good (until reset) and every
line goes to its safe level. A serial *monitor* opening the port is not enough —
it is the greeting that hands over, not the connection.

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
