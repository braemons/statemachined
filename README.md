# fsmd — the trial state machine

> **Status:** design only. Nothing is implemented yet. Read
> [`dev/PLAN.md`](dev/PLAN.md) — it is the whole project so far, and it is meant
> to be argued with.

**fsmd** is the part of a braemons rig that runs the *within-trial* state machine
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
  │ triald │                                       │ fsmd     │  host bridge
  └────────┘                                       │ (bridge) │
  ═══════════════════════════════════════════════  └────┬─────┘
                                                        │ USB CDC · NDJSON
  ┌─────────┬─────────┬─────────┬──────────┬────────────┴──────┐
  │ vstimd  │ soundd  │ optod   │  daqd    │  fsmd (firmware)  │
  └─────────┴─────────┴─────────┴──────────┴───────────────────┘
              coupled to each other by trigger edges
```

**The division of authority.** fsmd is the *timing* authority — it debounces
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
  `Arduino.h`; hardware is four functions behind a HAL. The same code runs on the
  host, which is what makes the tests real.

## Build & test

The core is plain C++17 with no `Arduino.h`, so it builds and runs on the host.
That is the fastest feedback loop in the repo and it needs no board attached.

```sh
make test        # build and run the core unit tests
make sanitize    # the same, under ASan and UBSan
make firmware    # build for the Uno R4 Minima
make upload      # flash it
make format      # clang-format in place
```

CI runs the unit tests under gcc and clang, under sanitizers, at `-O0` and `-O3`
(the cheapest way to catch a dependence on undefined behaviour), and compiles the
firmware for the reference board.

[`BUILD.md`](BUILD.md) has toolchain setup for Ubuntu 24.04, Fedora 44+ and WSL,
and a devcontainer that pins the same versions CI uses.

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

Not yet chosen — see *Open questions* §6 in the plan. The proposal is **GPLv3 for
the firmware** (Bpod's firmware is GPLv3, so borrowing from it decides this) and
**LGPLv3 for the host bridge**, so an experiment importing the bridge is not
placed under copyleft — the same split, and the same reason, as vstimd's client.

Copyright © 2026 Joscha Schmiedt, University of Bremen.
