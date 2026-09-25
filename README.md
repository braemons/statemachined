# statemachined — the trial state machine

> ## ⚠️ Alpha — `v0.3.0-alpha1`
>
> **Do not run an experiment on this.** It has never controlled a session with a
> subject in it, and it is not yet something a rig should depend on.
>
> What *is* real: the portable core, the wire protocol and the Uno R4 Minima HAL
> are implemented and tested — on the host, under sanitizers, and on a physical
> R4, which measured **122 767 Hz** against the 10 kHz target. The daemon — Rust
> since 0.3 —, its gRPC API and its web UI exist and drive whole trials against
> a board.
>
> What has **not** happened, and matters:
>
> - **No session has run against triald on a rig.** triald driving this daemon
>   is exercised end to end only in `contracts/e2e-tests/`, against the
>   firmware built for the host (M6).
> - **The board's data flash has never run on silicon.** Saving wiring, autorun
>   and the graph set is tested on the host and against a native build of the
>   same firmware; the RA4M1 path itself is unproven (M7).
> - **The packages have never been installed on a rig.** They build, they are
>   reproducible, and `contracts/e2e-tests/` installs them in a container; no
>   rig has one (M5).
>
> Pre-releases go to the braemons archive's `testing` suite, never `stable` —
> a `~` in the version is what keeps a rig tracking `stable` from being offered
> one. [`dev/PLAN.md`](dev/PLAN.md) is the argument for all of it, milestones at
> the end, and it is meant to be argued with.

**statemachined** is the part of a braemons rig that runs the *within-trial* state machine
on a microcontroller: it steps through a finite set of states, each with a map of
triggers to a next state, a timeout, and output actions, and it **names the trial
outcome**.

It is the participant that [triald](https://github.com/braemons/triald)'s plan
calls "the MCU" — the half of VStim's interval table that
[vstimd](https://github.com/braemons/vstimd)'s armed animations cannot express:
response windows, timeouts, reward, and an outcome.

```
   triald     configure / start (gRPC) · WatchTrace       slow bus · gRPC
  ┌────────┐  ◀────────────────────────────────▶  ┌───────────────┐
  │ triald │                                       │ statemachined │  the daemon
  └────────┘                                       │   (host)      │
  ═══════════════════════════════════════════════  └────┬──────────┘
                                                        │ USB CDC · COBS · protobuf
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
- **Terminal states name a `.tdr` outcome code** from triald's fixed
  taxonomy, which is a wire contract with years of recorded files behind it.
- **Cancellation is a forced transition** through the ordinary exit path, so
  every output a state raised is lowered by the same code that lowers it on any
  other transition. A valve cannot be left open by a graph that forgot something.
- **It can run with nothing attached.** A board remembers its wiring, its graph
  set and whether it should be arming its own trials, and comes up doing it after
  a power cut. The interval between trials is the dwell each terminal state
  declares — in the graph, because it is a paradigm decision that has to replay
  with the trial it followed; acted on only by a board driving itself, because
  who arms trials is a fact about the deployment. A terminal state declaring no
  dwell is where such a session stops. Greeting a board takes the rig back.
- **Portable core.** The engine, codec and protocol are plain C++17 with no
  `Arduino.h`; hardware is seven functions behind a HAL. The same code runs on the
  host, which is what makes the tests real.

## Build & test

The core is plain C++17 with no `Arduino.h`, so it builds and runs on the host.
That is the fastest feedback loop in the repo and it needs no board attached.

```sh
make test        # build and run the core unit tests
make rust-check  # the daemon: its tests, its generated types, clippy
make client      # the Python client: its stubs, lint, types, and against the daemon
make test-hardware TARGET=native   # the board suite, against the firmware built for this machine
make sanitize    # the core tests, under ASan and UBSan
make golden      # the tests at -O0 and -O3, for reproducibility
make firmware    # build for the Uno R4 Minima
make upload      # flash it
make format      # clang-format in place
make image       # the flashable image, with a manifest
make deb         # an installable package: the daemon, a unit, a udev rule
make ci          # everything CI runs
make help        # the full list
```

**The Makefile is the task runner, and CI calls these same targets** — versions
and flags are pinned in one place, so a job that goes red can be reproduced with
`make ci` rather than by reading a workflow file and retyping it.

CI runs the unit tests under gcc and clang, under sanitizers, at `-O0` and `-O3`
(the cheapest way to catch a dependence on undefined behaviour), compiles the
firmware for the reference board, checks the portable core has not reached for
`Arduino.h`, runs the daemon's tests and the client's against it, and runs the
hardware suite against the firmware built for the host. Only a board makes the
timing true: `make test-hardware TARGET=/dev/ttyACM0`.

[`BUILD.md`](BUILD.md) has toolchain setup for Ubuntu 24.04, Fedora 44+ and WSL,
and a devcontainer that pins the same versions CI uses.

## Try it on a board

Flash it, hand it a graph once, and it will run that graph with nothing plugged
into it — including after a power cut.

```sh
make upload
```

[`docs/operations/bringup.md`](docs/operations/bringup.md) is the step-by-step procedure, in the order
that makes each stage fail on its own before the next one depends on it.

With **nothing wired**, the on-board LED on D13 blinks once a second — the board
booted, the timer ISR fires, the scan loop turns. That is the whole of what a
board nobody has spoken to does: it holds no graph until one is uploaded,
because a device that runs a paradigm nobody uploaded is a hazard.

To make it visible, give it one. With two switches and three LEDs (wiring,
including the pull-downs you do need, in
[`docs/operations/hardware.md`](docs/operations/hardware.md)), upload `graphs/state-walk.json`, tell the
board to arm its own trials and save:

```sh
make bringup TARGET=/dev/ttyACM0     # greet it, and see what it says
# then, from the web UI or the API: upload state-walk, "let the board run
# itself", "save to the board"
```

The ready lamp lights, a press on the start switch walks one lamp across three
outputs at 500 ms a step, the trial ends as a `Hit`, and 1.5 s later it goes
again. Unplug the USB cable and it keeps going; power-cycle it and it comes back
doing the same thing, because the graph, the wiring and the instruction to run
it are in the board's own data flash.

**There is no demo mode any more, and that is the point.** This firmware used to
carry a paradigm compiled into it, so that a bench board did something watchable
before anything greeted it — which cost 4.6 KB of SRAM and meant two kinds of
image, one of which had the demo compiled out so that a rig could not quietly
run it while somebody believed it was running an experiment. A board that runs
a real uploaded graph out of its own storage is strictly better: what you watch
is evidence about the whole path rather than about a parallel one, the paradigm
is a file you can edit, and there is one binary to flash.

Every CI run publishes that binary as an artifact
(`statemachined-uno_r4_minima-<sha>`), so a board can be brought up without a
toolchain, with a `MANIFEST.txt` recording the version, commit, sizes and
checksums. The board reports the same version in its `hello_ack`, and the daemon
compares the two. `make image` builds
the same thing locally.

## Drive it from Python

The daemon owns the board; a script talks to the daemon. The client is
`client/python/`, published as `statemachined-client`, and it is the only
Python here:

```python
from statemachined_client import StatemachinedClient

rig = StatemachinedClient("rig-3.local")      # gRPC, on 8081
rig.upload_graph_set(["go-nogo", "two-alternative-forced-choice"])

rig.configure_trial(1, graph="go-nogo", cap_milliseconds=30_000)
rig.start_trial(1)
rig.wait_for_trial(1, timeout_s=35)

result = rig.read_trial_result(1)
print(result.outcome, [visit.state_name for visit in result.visits])
```

It installs `statemachinectl` as well, which is the bench instrument: `make
bringup ARGS="device"` asks a daemon what board it has, `ARGS="state"` what the
board is doing. On a bench with no board, `make bench-device` runs the firmware
built for this machine on `127.0.0.1:5300` and `make bench
TARGET=socket://127.0.0.1:5300` puts a daemon in front of it.

## Install it on a rig

`make packages` builds installable packages — the daemon, one binary at
`/usr/bin/statemachined` with its web UI inside it, a systemd unit, a udev rule that names the
board by VID/PID instead of granting the daemon every serial port on the box, a
conffile describing what the rig is, and the flashable firmware. `.deb` and
`.rpm`, amd64 and arm64, the last of those being a Raspberry Pi 5 running
beside vstimd and triald.

Each is built inside a container pinned by digest, and building the same commit
twice gives the same bytes — `make -C packaging repro` checks it, and so does
CI. A `v*` tag publishes the lot on a GitHub Release.

The package is deliberately **enabled but not started** on a first install:
starting it greets the board, and greeting takes the rig — a board that was
running trials on its own would stop. See
[`packaging/README.md`](packaging/README.md).

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
The Python client, `client/python/`: [LGPLv3-or-later](client/python/LICENSE)**,
so an experiment importing it is not placed under copyleft — the same split, and
the same reason, as vstimd's client. **The daemon — `daemon/`, and the panels
in `client/web/` it serves: [AGPLv3-or-later](daemon/LICENSE.AGPL)**, like
vstimd and triald: it is a network service, and whoever runs a modified one for
others owes them its source. The split is by *what the code is*, not by
directory. Every source file carries an
`SPDX-License-Identifier`.

The GPL here is a *choice*, not an inheritance: no Bpod source is copied,
translated or adapted anywhere in this repository — the references to it in the
code are comments comparing designs. We take the licence its authors chose
because we take their ideas. [`NOTICE`](NOTICE) records that, and the one piece
of third-party code in the tree (doctest, MIT, test-only).

Copyright © 2026 Joscha Schmiedt, University of Bremen.
