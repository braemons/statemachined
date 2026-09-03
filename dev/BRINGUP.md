# statemachined — bringing up a board

The procedure for putting this firmware on an Arduino Uno R4 Minima for the
first time, in the order that makes each stage fail loudly on its own before
anything below it depends on it.

Wiring tables and the per-board line map live in [`HARDWARE.md`](HARDWARE.md);
this page is the sequence. The wire protocol is [`PROTOCOL.md`](PROTOCOL.md).

**What this is for.** Everything in this repository is either tested on the host
or tested on an emulated board under Renode, and both are real tests of the
*logic*. Neither says anything about timing: Renode runs on virtual time, so a
scan there takes exactly as long as it is told to. The numbers this page
collects are the ones no amount of testing without hardware can produce, and
`PLAN.md` calls M3 the milestone that decides whether the design is right.

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

**Take the `bench` image.** The `rig` image is built with `-DSTATEMACHINED_DEMO=0` and has
demo mode compiled out, so every step up to §4 will look like a dead board.

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

| What | statemachined line | Pin | Wire it as |
|---|---|---|---|
| Start switch | input 0 | **D2** | switch to **5 V**, plus a **10 kΩ pull-down to GND** |
| Abort switch | input 1 | **D3** | the same |
| Step LEDs 1-5 | outputs 0-4 | **D10, D11, D12, A0, A1** | anode to pin, cathode through **220-330 Ω** to GND |
| Ready / done lamp | output 7 | **A4** | the same |
| Alive heartbeat | *not a line* | **D13** | nothing; it is the on-board LED |

**The pull-downs are not optional.** `hal::init()` sets inputs to `INPUT`, not
`INPUT_PULLUP`, deliberately: a rig's TTL sources drive both ways and a pull-up
would fight them. A switch with nothing else on the pin leaves it floating when
open, and a floating input picks up enough noise to start and abort trials on its
own. If you have no resistors to hand, a jumper from 5 V touched to D2 is bouncy
but unambiguous.

Wire the LEDs and the start switch first. Adding the abort switch afterwards
isolates a fault to one half of the picture.

---

## 3. Watch the demo

Press reset, then, in order:

1. **A4 lights and stays lit.** The `wait` state's entry action. If this does not
   happen, the LED or its resistor is wrong — the firmware has not reached
   anything else yet.
2. **Press start.** One LED walks D10 → D11 → D12 → A0 → A1, **500 ms each**.
   Count along: 2.5 s end to end.
3. **A4 comes back on.** That is the terminal `Hit` state's lamp. The next trial
   arms 1.5 s later.
4. **Press start, then abort mid-walk.** Everything stops at once and D10 lights
   as the `Cancelled` lamp.

Two behaviours that are correct and look like faults:

* **Holding start down does not re-trigger.** A transition fires on its
  predicate's *rising edge*, so the switch has to be released and pressed again.
  It is the same rule that stops a lever the animal is already holding from
  ending a trial the instant it begins.
* **One LED at a time, never two.** Exiting a state lowers everything that state
  raised, by the same code that lowers it on any other transition. Two lit at
  once would be a real finding — report it.

Erratic or self-starting chases are the pull-downs, not the firmware.

**With a scope**, this is where the first numbers that are not arithmetic live:
probe D2 and D10 together for input-to-output latency, and any step LED for the
500 ms dwell.

---

## 4. Talk to it, and read the number this is all for

Every line carries a CRC-16/CCITT-FALSE, so typing JSON into a serial monitor
gets no reply. Use the repository's own bench instrument, which frames commands
with the same helper CI drives the emulated board with
([`tools/bringup/`](../tools/bringup/README.md)):

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

> **`scan_hz` in the `hello_ack` is the number M3 is waiting on.**

It is measured at boot rather than declared — 2000 repetitions of reading and
conditioning the pins, timed — so it is what the board actually achieves, not
what the design hoped for. It is a *floor*: it covers the pins only, and
evaluating a state's transitions sits on top of it and depends on the graph.
Renode reports something in the hundreds of kHz here and it means nothing, since
virtual time is not time.

Sending `hello` also **ends demo mode for good until the next reset**. Every line
goes to its safe level and D13 stops blinking. That handover is deliberate: a rig
must never be able to run the demo while somebody believes it is running an
experiment. Note that *opening* the port is not enough — a serial monitor does
that — it is the greeting that hands over. Only `hello` and `report` greet the
board; `make bringup ARGS="monitor"` watches the link and sends nothing, which
is how you look at a board that is still running the demo.

---

## 5. Prove the pins reach the line numbers

```sh
make bringup ARGS="state"
```

If the board has not been greeted since it was reset it answers `not_ready`,
because nothing but `hello` is accepted before a session exists. Use
`ARGS="--hello state"` — which ends demo mode, as §4 says.

`"io":{"in":N,"out":M}` is the only way anything outside the device can check
that a graph's line numbers reach the pins somebody wired, because there is no
read-back path. The tool prints both as a row of bits with line 0 on the left
and names the pins from `HARDWARE.md`:

```
  in             1.......   high: 0 (D2)
  out            .......1   high: 7 (A4)
```

Hold the start switch and ask again: `in` goes from `0` to `1`. Hold both
switches: `3`. `ARGS="watch"` polls it a few times a second so you can do that
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
`firmware/src/main.cpp`; the numbers are in [`HARDWARE.md`](HARDWARE.md).

A board reporting *far* more than that, or a `worst_gap` in the hundreds, is
still a finding. So is any non-zero `scan.tx_stalls`, which means a reply had to
wait for the wire because the outbound queue was full.

---

## 6. Fail-safe, physically

With a trial in flight, **pull the USB cable.** On the USB CDC build
`hal::link_up()` goes false the moment the port closes; the session cancels
through the ordinary exit path and drives every line to its safe level. Watch the
LEDs go dark, on a meter if you want it recorded.

"The valve actually closed" is a different claim from "the test asserted it
closed", and only one of them is checked here.

One cosmetic wart so it does not surprise anybody: closing a serial monitor
*during demo mode* trips the same fail-safe, blanking the demo's lamps until the
next state change — up to 500 ms, or until the next trial if it was sitting in
`wait`. Demo-only and harmless.

---

## What to write down

Three numbers settle M3, and everything after it assumes they held:

| | |
|---|---|
| `scan_hz` from `hello_ack` | against the 10 kHz target |
| `scan.overruns` under link load | zero, or climbing |
| Scope: D2 → D10 latency, and the step dwell | against 500 ms |

The first two come out of one command, already as a markdown table:

```sh
make bringup ARGS="report" > /tmp/m3.md
```

Record them in [`HARDWARE.md`](HARDWARE.md) under the board they were measured
on, the way the RAM figures are recorded there — with the scope numbers added by
hand, since no amount of serial traffic can produce those. A measurement that
stays in somebody's terminal is one the next person has to take again.
