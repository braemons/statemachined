# statemachined — the trial state machine

> **Status:** the portable core, the wire protocol and the Uno R4 Minima HAL are
> implemented and tested — on the host, and on an emulated board under Renode.
> **No physical board has run this yet:** the RAM budget is measured, the 10 kHz
> scan rate is not. The host bridge to triald is next and does not exist yet.
> [`dev/PLAN.md`](dev/PLAN.md) is still the argument for all of it, milestones
> at the end, and it is meant to be argued with.

**statemachined** is the part of a braemons rig that runs the *within-trial* state machine
on a microcontroller: it steps through a finite set of states, each with a map of
triggers to a next state, a timeout, and output actions, and it **names the trial
outcome**.

It is the participant that [triald](https://github.com/braemons/triald)'s plan
calls "the MCU" — the half of VStim's interval table that
[vstimd](https://github.com/braemons/vstimd)'s armed animations cannot express:
response windows, timeouts, reward, and an outcome.

```
   triald            configure / arm / result            slow bus · HTTP+JSON
  ┌────────┐  ◀────────────────────────────────▶  ┌──────────┐
  │ triald │                                       │ statemachined     │  host bridge
  └────────┘                                       │ (bridge) │
  ═══════════════════════════════════════════════  └────┬─────┘
                                                        │ USB CDC · NDJSON
  ┌─────────┬─────────┬─────────┬──────────┬────────────┴──────┐
  │ vstimd  │ soundd  │ optod   │  daqd    │  statemachined (firmware)  │
  └─────────┴─────────┴─────────┴──────────┴───────────────────┘
              coupled to each other by trigger edges
```

**The division of authority.** statemachined is the *timing* authority — it debounces
inputs, timestamps in its own clock, drives the valve, and names the outcome.
triald is the *decision* authority — it chooses the trial type, decides whether
an outcome was *accepted*, and records what happened. **The firmware never learns
the trial type.**

## What is distinctive

- **Triggers are predicates over the whole input word**, not edges on one line.
  Three bitmasks (`all` / `any` / `none`) firing on the predicate's own rising
  edge, so *"both levers held for 200 ms while the abort line is low"* is one
  condition rather than hand-rolled bookkeeping.
- **Randomised timings are drawn on the device** from a host-seeded deterministic
  PRNG — uniform, truncated exponential, discrete choice — with every realised
  duration reported back. Seeded so it replays; reported so it is evidence.
- **Terminal states name a `.tdr` outcome code** from triald's fixed eleven-code
  taxonomy, which is a wire contract with years of recorded files behind it.
- **Cancellation is a forced transition** through the ordinary exit path, so
  every output a state raised is lowered by the same code that lowers it on any
  other transition. A valve cannot be left open by a graph that forgot something.
- **Portable core.** The engine, codec and protocol are plain C++17 with no
  `Arduino.h`; hardware is seven functions behind a HAL. The same code runs on the
  host, which is what makes the tests real.

## Build & test

The core is plain C++17 with no `Arduino.h`, so it builds and runs on the host.
That is the fastest feedback loop in the repo and it needs no board attached.

```sh
make test        # build and run the core unit tests
make sanitize    # the same, under ASan and UBSan
make golden      # the tests at -O0 and -O3, for reproducibility
make firmware    # build for the Uno R4 Minima
make upload      # flash it
make emulate     # run the firmware under Renode
make format      # clang-format in place
make image       # both flashable images, with a manifest
make ci          # everything CI runs, except emulation
make help        # the full list
```

**The Makefile is the task runner, and CI calls these same targets** — versions
and flags are pinned in one place, so a job that goes red can be reproduced with
`make ci` rather than by reading a workflow file and retyping it.

CI runs the unit tests under gcc and clang, under sanitizers, at `-O0` and `-O3`
(the cheapest way to catch a dependence on undefined behaviour), compiles the
firmware for the reference board, checks the portable core has not reached for
`Arduino.h`, and boots the firmware on an **emulated** Uno R4 Minima under Renode
so that `firmware/hal/` is covered too — the pin map, the port registers, the
timer ISR and a whole session over a real UART peripheral. Emulation runs on
virtual time, so it makes the HAL correct; only a board makes the timing true.
See [`emulation/README.md`](emulation/README.md).

[`BUILD.md`](BUILD.md) has toolchain setup for Ubuntu 24.04, Fedora 44+ and WSL,
and a devcontainer that pins the same versions CI uses.

## Try it on a board

You do not need the host bridge, or a host at all, to see this run. Flash it and
the board runs a built-in demo graph until something greets it:

```sh
make upload
```

[`dev/BRINGUP.md`](dev/BRINGUP.md) is the step-by-step procedure, in the order
that makes each stage fail on its own before the next one depends on it.

With **nothing wired**, the on-board LED on D13 blinks once a second — the board
booted, the timer ISR fires, the scan loop turns. With two switches and six LEDs
(wiring, including the pull-downs you do need, in
[`dev/HARDWARE.md`](dev/HARDWARE.md)) the ready lamp lights, a press on the start
switch walks one LED across five outputs at 500 ms a step, and the trial ends as
a `Hit` — or as `Cancelled` if you press abort on the way past.

Every CI run publishes a flashable image as an artifact
(`statemachined-uno_r4_minima-<sha>`), so a board can be brought up without a toolchain:
a **bench** image with demo mode on, a **rig** image with it compiled out, and a
`MANIFEST.txt` recording the commit, sizes and checksums — a board in a rack
cannot be asked which commit it is running. `make image` builds the same thing
locally.

It is the real engine on a real graph: the same `TrialRunner`, the same
`validate()`, the same conditioned input word, built by
`firmware/core/demo/demo_graph.cpp` and run on the host by its own test. It is
**not** a fallback paradigm — the first `hello` ends it for good and hands every
line back, so a rig cannot quietly run the demo while somebody believes it is
running an experiment. A deployed build can drop it entirely with
`-DSTATEMACHINED_DEMO=0`, which is worth 4.6 KB of SRAM.

## Target hardware

| | |
|---|---|
| **Arduino Uno R4 Minima** | Reference target. 32 KB RAM, and the constraint that keeps the design honest |
| Teensy 4.1 | The headroom target, and Bpod's own board |
| ESP32 | WiFi and BT stay off in the trial loop |
| native | Unit tests and the simulator — the same `core/`, on the host |

## Prior art

**[Bpod](https://sanworks.io/shop/products.php?productFamily=bpod)** (Sanworks
LLC, Josh Sanders, out of the Kepecs lab) is the closest existing system and the
direct intellectual ancestor of the model here. The idea of a state matrix as the
per-trial unit of configuration is Bpod's, and we are not pretending otherwise.
`dev/PLAN.md` has a section on exactly what we take from its firmware, what we do
differently, and why. If you already run Bpod, run Bpod.

Also: **VStim** (Andreas Kreiter, Cognitive Neurophysiology Lab, Bremen), whose
`ExpCtrl`/`Interval`/`TimeSqz` this decomposes; **pyControl** (Thomas Akam);
**Autopilot** (Jonny Saunders); and the **MonkeyLogic** lineage.

## License

**Firmware, core, tests and tools: [GPLv3-or-later](LICENSE).
Host bridge: [LGPLv3-or-later](bridge/LICENSE)**, so an experiment importing the
bridge is not placed under copyleft — the same split, and the same reason, as
vstimd's client. Every source file carries an `SPDX-License-Identifier`.

The GPL here is a *choice*, not an inheritance: no Bpod source is copied,
translated or adapted anywhere in this repository — the references to it in the
code are comments comparing designs. We take the licence its authors chose
because we take their ideas. [`NOTICE`](NOTICE) records that, and the one piece
of third-party code in the tree (doctest, MIT, test-only).

Copyright © 2026 Joscha Schmiedt, University of Bremen.
