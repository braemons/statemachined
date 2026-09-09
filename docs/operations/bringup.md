# statemachined — bringing up a board

The procedure for putting this firmware on an Arduino Uno R4 Minima for the
first time, in the order that makes each stage fail loudly on its own before
anything below it depends on it.

Wiring tables and the per-board line map live in [`hardware.md`](hardware.md);
this page is the sequence. The wire protocol is [`protocol.md`](../reference/protocol.md).

**What this is for.** Everything in this repository is either tested on the host
or tested on an emulated board under Renode, and both are real tests of the
*logic*. Neither says anything about timing: Renode runs on virtual time, so a
scan there takes exactly as long as it is told to. This page is the sequence for
putting the firmware on a board and confirming, by eye and with a meter, the
things no amount of testing without hardware can confirm.

The numbers themselves are no longer collected by hand — §5a is the suite that
asserts them, and the measurements it holds are recorded in
[`hardware.md`](hardware.md).

---

## 0. Get the firmware

Either build it:

```sh
make firmware
```

or skip the toolchain entirely and take the image CI publishes on every run:

```sh
gh run download --name statemachined-uno_r4_minima-<sha>
```

Check `MANIFEST.txt` against the commit you believe you are testing. A board in
a rack cannot be asked what it is running, which is why that file exists.

There is one image. There used to be two -- a bench build carrying a demo
paradigm and a rig build with it compiled out -- and there is now nothing to
choose between: a board runs whatever graph it was given, out of its own
storage, and holds none until it is given one.

---

## 1. Flash it with nothing wired

```sh
make upload
```

If the port is not found, double-tap the reset button to force the bootloader and
try again. The board enumerates as `/dev/ttyACM0` on Linux.

> **Expect: D13 blinks briefly, once a second.**

Nothing is connected, so that is the entire test — and it covers the three things
that fail first: the board booted, `hal::start_scan_timer()` returned true, and
`loop()` is consuming ticks.

A dark D13 means a dead board rather than a shy one. `setup()` halts deliberately
with every line at its safe level if the timer will not start, because a board
that is obviously dead is a much better failure than one that looks healthy and
silently never scans. There is nothing to read on the serial port in that state
either: the fault is before the link is serviced.

**Stop here if it does not blink.** Nothing below will work.

---

## 2. Wire it

These are the lines `configs/uno-r4-minima-bench.config.json` maps, so a board
wired this way can run every example graph in `graphs/` unchanged.

| What | statemachined line | Pin | Wire it as |
|---|---|---|---|
| Start switch | input 0 | **D2** | switch to **5 V**, plus a **10 kΩ pull-down to GND** |
| Abort switch | input 1 | **D3** | the same |
| Ready lamp | output 0 | **D10** | anode to pin, cathode through **220-330 Ω** to GND |
| Cue lamp | output 1 | **D11** | the same |
| Error lamp | output 2 | **D12** | the same |
| Reward valve | output 3 | **A0** | an LED will do; on a rig it is the driver |
| Alive heartbeat | *not a line* | **D13** | nothing; it is the on-board LED |

**The pull-downs are not optional.** `hal::init()` sets inputs to `INPUT`, not
`INPUT_PULLUP`, deliberately: a rig's TTL sources drive both ways and a pull-up
would fight them. A switch with nothing else on the pin leaves it floating when
open, and a floating input picks up enough noise to start and abort trials on its
own. If you have no resistors to hand, a jumper from 5 V touched to D2 is bouncy
but unambiguous.

Wire the lamps and the start switch first. Adding the abort switch afterwards
isolates a fault to one half of the picture.

---

## 3. Nothing happens, and that is correct

Press reset. **D13 blinks once a second and every other pin stays dark.**

That is the whole of what a board nobody has spoken to does. It holds no graph
until one is uploaded, because a device that runs a paradigm nobody uploaded is
a hazard -- so there is nothing for it to run, and the heartbeat is how it says
it booted, started its timer and is scanning.

A dark D13 is the finding here: the fault is before the link, and §4 will not
help. A blinking D13 with a lamp also lit means either a stored graph is already
running (§7 -- this board has been set up before) or the lamp is wired to
something it should not be.

Watching a paradigm run is §7, after there is one on the board. It is worth
knowing what it will look like: the ready lamp lights, a press on the start
switch walks one lamp across D10 -> D11 -> D12 and round again at **500 ms a
step**, the trial ends as a `Hit`, and 1.5 s later it goes again. That is
`graphs/state-walk.json`, and the point of it is that it is a file you uploaded
rather than a light show compiled into the firmware.

**With a scope**, this is where the first numbers that are not arithmetic live,
once §7 has given the board a graph: probe D2 and D10 together for
input-to-output latency, and any step lamp for the 500 ms dwell.

Two behaviours that are correct and look like faults, when you get there:

* **Holding start down does not re-trigger.** A transition fires on its
  predicate's *rising edge*, so the switch has to be released and pressed again.
  It is the same rule that stops a lever the animal is already holding from
  ending a trial the instant it begins.
* **One lamp at a time, never two.** Exiting a state lowers everything that
  state raised, by the same code that lowers it on any other transition. Two lit
  at once would be a real finding -- report it.

Erratic or self-starting chases are the pull-downs, not the firmware.

---

## 4. Talk to it

Every line carries a CRC-16/CCITT-FALSE, so typing JSON into a serial monitor
gets no reply. Use the repository's own bench instrument, which frames commands
with the same helper CI drives the emulated board with
([`python/`](../../python/README.md)):

```sh
make bringup ARGS="hello"
```

`uv` builds its environment on first use; nothing lands in the system Python.
The board is `/dev/ttyACM0` unless you say otherwise —
`make bringup TARGET=... ARGS=...`, and `TARGET` also takes a `host:port` or a
`socket://` URL for a device that is on a network rather than a cable.

```
hello_ack
  seed           443ADD5C803378B8
  board          uno_r4_minima   fw 0.1.0   proto 1
  lines          8 in, 8 out
  scan_hz        11234  (above the 10000 Hz target)
  graph          none
  caps           {"max_line": 512, ...}
```

> **`scan_hz` in the `hello_ack` is what the board actually achieves.**

It is measured at boot rather than declared — 2000 repetitions of reading and
conditioning the pins, timed — so it is what the board actually achieves, not
what the design hoped for. It is a *floor*: it covers the pins only, and
evaluating a state's transitions sits on top of it and depends on the graph.
Renode reports something in the hundreds of kHz here and it means nothing, since
virtual time is not time.

Sending `hello` also **takes the rig**: a board that was arming its own trials
out of its own storage (§7) stops doing so, the run in flight is cancelled
through the ordinary exit path, and its result is still reported. The stored
setting survives, so the next boot comes up self-driving again; restarting it in
this session takes another `autorun`. That asymmetry is deliberate — a daemon
that crashed must not be able to leave a board rewarding an animal nobody is
watching. Note that *opening* the port is not enough — a serial monitor does
that — it is the greeting that hands over. Only `hello` and `report` greet the
board; `make bringup ARGS="monitor"` watches the link and sends nothing, which
is how you look at a board that is still running.

---

## 5. Prove the pins reach the line numbers

First, ask the board which pin each line *is*. It answers out of the same table
its firmware calls `pinMode()` over, so this is the board's own word and not
this tool's:

```sh
make bringup ARGS="--hello pins"
```

```
  inputs
    line 0   D2
    ...
  outputs
    line 3   A0
```

Firmware older than `PROTOCOL.md` §3.6 answers `no_pin_map` here, which is not
a failure — it means the daemon will fall back to its own table and label every
pin it shows as **assumed**. Flash current firmware if you would rather it were
checked.

Then watch the lines move:

```sh
make bringup ARGS="state"
```

If the board has not been greeted since it was reset it answers `not_ready`,
because nothing but `hello` is accepted before a session exists. Use
`ARGS="--hello state"` — which takes the rig, as §4 says.

`"io":{"in":N,"out":M}` is the only way anything outside the device can check
that a graph's line numbers reach the pins somebody wired, because there is no
read-back path. The tool prints both as a row of bits with line 0 on the left
and names the pins from `HARDWARE.md`:

```
  in             1.......   high: 0 (D2)
  out            .......1   high: 7 (A4)
```

Hold the start switch and ask again: `in` goes from `0` to `1`. Hold both
switches: `3`. **This is the only check that cannot be done in software**: the
`pins` command settles what the firmware believes, and this settles whether the
wire is in that hole. `ARGS="watch"` polls it a few times a second so you can do that
with both hands on the wires.

The same reply carries `scan.overruns` and `scan.worst_gap` — scan periods that
went by with no scan in them, counted rather than absorbed. `ARGS="load"` reads
them, sends 200 pings back to back, and reads them again; it exits non-zero if
the count moved.

> **If overruns climb steeply under link traffic, that is a finding.**

They did, on the reference board, and the fix is in: the scan runs in the timer
ISR and the foreground holds the engine only while it is parsing a command. What
remains is that hold — about **3 periods per command** on an Uno R4 Minima,
against 9.9 before — and it is bounded by our own parse rather than by whatever
the USB stack is doing. The measurement, and the reasoning, are at the top of
`firmware/src/main.cpp`; the numbers are in [`hardware.md`](hardware.md).

A board reporting *far* more than that, or a `worst_gap` in the hundreds, is
still a finding. So is any non-zero `scan.tx_stalls`, which means a reply had to
wait for the wire because the outbound queue was full.

---

## 5a. The same checks, automated

Everything from §4 and §5 that does not need a person's eyes is a test suite:

```sh
make test-hardware                      # or TARGET=host:5000, as above
```

Connect the board and run it. It greets the device once — **which takes the
rig**, as §4 says — and then asserts what the sections above ask you to read:
`scan_hz` against the 10 kHz target, what a command costs the scan, a drawn
duration against the board's own clock, the framing rules against lines a
well-behaved host would never send, and a whole trial's result arriving intact
while the scan runs in the timer ISR.

It is **not** part of `make ci`. A target that fails on every machine without a
board attached is a target people learn to ignore.

### The loopback harness

What the suite cannot do without jumper wires is drive the board's *inputs*.
Add them and two more things run: the predicate tests, which fire a transition
from a condition over several lines, and the timing suite in
`test_timing_accuracy.py`, which measures how long the board takes to answer a
line it drove itself.

**Eight wires, output line _n_ to input line _(n + 4) mod 8_:**

| From | | To | | From | | To |
|---|---|---|---|---|---|---|
| **D10** (output 0) | → | **D6** (input 4) | | **A1** (output 4) | → | **D2** (input 0) |
| **D11** (output 1) | → | **D7** (input 5) | | **A2** (output 5) | → | **D3** (input 1) |
| **D12** (output 2) | → | **D8** (input 6) | | **A3** (output 6) | → | **D4** (input 2) |
| **A0** (output 3) | → | **D9** (input 7) | | **A4** (output 7) | → | **D5** (input 3) |

The board then drives its own inputs through a graph's entry actions, one scan
later, which is how "both levers released and pressed again within the same
millisecond" becomes something a test can do. Without the wires those tests skip
and print this table; nothing else changes.

**Why the shift, and not output _n_ to input _n_.** A straight-through harness
cannot tell a correct board from one whose reported input word is secretly the
output word — raise output 0, see bit 0, pass. Under the shift every output has
a unique and non-obvious expected input bit, so that failure, and any rotation
or off-by-one in either pin table, fails rather than passing for the wrong
reason. It also covers all sixteen lines: before it, thirteen of the board's
pins had never once been proven to be the pin the table claims.

**It replaces the §2 bench switches rather than sitting beside them.** The wires
to D2 and D3 land on the pins §2 wires as a contact to 5 V, and an output
driving low against a closed switch is a short. Take the switches off while the
harness is on, or put **1 kΩ in series** in those two wires — the inputs are
high-impedance, so it costs nothing logically. Sharing the *output* pins with
the bench lamps is harmless: a pin drives an LED and a jumper equally well.

A run takes about 90 seconds and leaves the device idle.

### Whole sessions, against this board

`make test-hardware` drives the board one command at a time, the way a bench
instrument does. To run whole *sessions* against it — several trials, through
the daemon's HTTP API and through `StatemachinedDevice`, with the paradigms
answering their own response windows through the harness above:

```sh
make test-runs-hardware TARGET=/dev/ttyACM0
```

Those are the same tests `make test-runs` runs with no board attached, against
the firmware built for this machine. That is the point of them: the far end is
a fixture, so a session that passes in CI and fails here has found something
about the board rather than about the test.

---

## 6. Fail-safe, physically

With a trial in flight, **pull the USB cable.** On the USB CDC build
`hal::link_up()` goes false the moment the port closes; the session cancels
through the ordinary exit path and drives every line to its safe level. Watch the
LEDs go dark, on a meter if you want it recorded.

"The valve actually closed" is a different claim from "the test asserted it
closed", and only one of them is checked here.

One exception, and it is the point of §7 rather than a wart: a board that was
**told** to arm its own trials does not fail safe when the cable goes, because
for that board an unplugged cable is the expected end of "upload a paradigm,
then detach" rather than a fault. Nothing else behaves that way, and it takes an
explicit `autorun` to get there.

---

## 7. The daemon and the web UI, in front of the board

Everything above talks to the board with one command at a time. This runs the
whole host half against it -- the API of [`api.md`](../reference/api.md), the trace, and the
six panels of [`daemon.md`](../developer/daemon.md) §5 -- so that what you are looking at in
a browser is a real device.

```sh
make bench                       # /dev/ttyACM0, or make bench TARGET=...
```

Then open **http://127.0.0.1:8081/**. The daemon greets the board on startup,
pushes the wiring from the line map in
`python/bench/statemachined_bench_rig_config.toml`, and seeds its graph store
from `graphs/` into `build/bench/graphs` -- a copy, so deleting a graph in the
browser does not delete an example from the repository. Greeting takes the rig,
as it does anywhere else.

**The board must be running current firmware.** An image from before the graph
set exists answers `hello` perfectly well and then refuses the upload: the tell
is `max_graphs` missing from `caps`, and `max_path` at 64 rather than 255.
`make upload` fixes it.

```sh
make bringup ARGS="hello"        # caps, before wondering why an upload failed
```

### 7a. Give it a graph, and let go of it

This is what §3 deferred, and it is what replaced demo mode. In the **Session**
panel:

1. Tick **state-walk** and upload it as the session's set.
2. Press **arm and start** once, with the start switch in reach. The ready lamp
   lights; press start and one lamp walks D10 -> D11 -> D12 and round again at
   500 ms a step; the trial ends as a `Hit`. That is the graph you just
   uploaded running on the board's own clock.
3. Under **Run without the daemon**, press **let the board run itself**. It
   arms its own trials from here, waiting out the 1.5 s dwell `state-walk`'s
   `Done` state declares between them (`relight_after`).
4. **Pull the USB cable.** It keeps going. Nothing was cancelled and nothing
   failed safe, because this board was told to do this -- which is exactly why
   it takes a command of its own rather than being somewhere a dropped cable
   can arrive at by accident.
5. Plug it back in, press **take the rig back**, then **save to the board**, and
   power-cycle it. It comes back walking, with nothing attached: the graph, the
   wiring and the instruction to run it are in its data flash.

The reply to a save carries `write_count`. That is flash wear made visible --
the RA4M1's data flash is good for about 100,000 erase cycles -- and it is worth
glancing at on a board that has been through a lot of bring-ups.

If you would rather do it without the browser:

```sh
curl -X PUT localhost:8081/api/device/autorun \
     -H 'content-type: application/json' \
     -d '{"enabled":true,"graph_name":"state-walk","start_now":false}'
curl -X POST localhost:8081/api/device/save
```

`start_now: false` is the order that works while setting a rig up: a save is
refused on a board that is running, and a board arming its own trials is never
idle, so the intent is written down first and the power cycle is what acts on
it.

### With no board at all

The same firmware, built for this machine, on a TCP port -- not a mock, and not
a second protocol implementation, but `firmware/native/` driven by an ordinary
loop instead of a timer ISR. Two terminals:

```sh
make bench-device                             # socket://127.0.0.1:5300
make bench TARGET=socket://127.0.0.1:5300
```

What this cannot tell you is anything the board is for: no pin reaches a wire,
no scan has a deadline, and `scan_hz` is whatever this machine managed. It is
for the UI and the API, and the section above is for the rig.

---

## What to write down

Nothing, on an ordinary bring-up. The three figures this page used to ask you to
record by hand — the scan rate, what link traffic costs the scan, and a served
duration against its drawn one — are asserted by `make test-hardware`, and the
reference board's measurements are in
[`hardware.md`](hardware.md) under the board they were taken on.

What still has no automated path is anything needing an instrument:

```sh
make bringup ARGS="report" > /tmp/board.md
```

writes the link-side figures as a markdown table, and a scope on D2 and D10
gives the input-to-output latency that the board's own clock cannot honestly
measure about itself. If you take either, add it to
[`hardware.md`](hardware.md) beside the others — a measurement that stays in
somebody's terminal is one the next person has to take again.
