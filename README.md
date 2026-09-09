# statemachined — the trial state machine

> ## ⚠️ Alpha — `v0.1.0-alpha1`
>
> **Do not run an experiment on this.** It has never controlled a session with a
> subject in it, and it is not yet something a rig should depend on.
>
> What *is* real: the portable core, the wire protocol and the Uno R4 Minima HAL
> are implemented and tested — on the host, under sanitizers, on an emulated
> board under Renode, and on a physical R4, which measured **122 767 Hz**
> against the 10 kHz target. The daemon, its HTTP API and its web UI exist and
> drive whole trials against a board.
>
> What has **not** happened, and matters:
>
> - **No session has ever run against triald.** The daemon reports outcomes to
>   an interface nothing has exercised end to end (M6).
> - **The board's data flash has never run on silicon.** Saving wiring, autorun
>   and the graph set is tested on the host and against a native build of the
>   same firmware; the RA4M1 path itself is unproven (M7).
> - **The packages have never been installed on a machine.** They build, they
>   are reproducible, and no rig has one (M5).
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
make test-python # the Python package: unit, integration, and the shipped commands
make sanitize    # the same, under ASan and UBSan
make golden      # the tests at -O0 and -O3, for reproducibility
make firmware    # build for the Uno R4 Minima
make upload      # flash it
make emulate     # run the firmware under Renode
make format      # clang-format in place
make image       # the flashable image, with a manifest
make deb         # an installable package: the daemon, a unit, a udev rule
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

Flash it, hand it a graph once, and it will run that graph with nothing plugged
into it — including after a power cut.

```sh
make upload
```

[`dev/BRINGUP.md`](dev/BRINGUP.md) is the step-by-step procedure, in the order
that makes each stage fail on its own before the next one depends on it.

With **nothing wired**, the on-board LED on D13 blinks once a second — the board
booted, the timer ISR fires, the scan loop turns. That is the whole of what a
board nobody has spoken to does: it holds no graph until one is uploaded,
because a device that runs a paradigm nobody uploaded is a hazard.

To make it visible, give it one. With two switches and three LEDs (wiring,
including the pull-downs you do need, in
[`dev/HARDWARE.md`](dev/HARDWARE.md)), upload `graphs/state-walk.json`, tell the
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
toolchain, with a `MANIFEST.txt` recording the commit, sizes and checksums — a
board in a rack cannot be asked which commit it is running. `make image` builds
the same thing locally.

## Drive it from Python

`python/` is one package with three tiers, and which one you install says how
you mean to drive a board.

```sh
pip install statemachined            # the documents, and talking to a daemon
pip install 'statemachined[device]'  # + open the serial port yourself
pip install 'statemachined[serve]'   # + be the daemon
```

**Through a daemon**, when something other than your script owns the board — a
rig, where `statemachined serve` is holding it and a web UI and triald are
watching too:

```python
from statemachined.client import StatemachinedClient

with StatemachinedClient("http://rig-3.local:8081") as rig:
    rig.session.upload_graph_set(["go-nogo", "2afc"])

    with rig.trace.subscribe("my-experiment") as stream:
        rig.trial.configure(1, graph="go-nogo", cap_milliseconds=30_000)
        rig.trial.start(1)
        stream.wait_for_trial(1, timeout_seconds=35)

    for entry in rig.trace.for_trial(1):
        print(entry["kind"], entry.get("state_name"), entry.get("outcome"))
```

**Or straight at the board**, on a bench, with no daemon anywhere:

```python
from statemachined.device import StatemachinedDevice
from statemachined.model.graph_definition import GraphDefinition
from statemachined.model.line_map import LineMap

board = StatemachinedDevice("/dev/ttyACM0", line_map=LineMap.model_validate(wiring))
board.connect_and_greet()
board.push_wiring()
board.upload_graph_set([GraphDefinition.model_validate(go_nogo)], set_version=1)

result = board.run_trial_to_completion(1, "go-nogo", cap_milliseconds=30_000)
# `.name`, not the member: TrialOutcome is an IntEnum and since Python 3.11
# those print as their number. The `.tdr` taxonomy crosses the wire by name.
print(result.outcome.name, [visit.state_name for visit in result.visits])
```

**Two classes and not one facade with two backends**, because the difference is
not the transport. A daemon keeps a trace ring, takes named recordings off it,
holds a graph store and saved configs on disk, and can say who else is watching
— all of which exist because it *outlives the script that spoke to it*. A direct
connection has no ring to record from: your process was the only listener, and
what it did not keep is gone. One facade would have to answer
`rig.recordings.start()` on both, and on one of them the answer would be a
fiction.

What they do share is the trial loop, the graph set, the wiring, autorun and
save — because those are the board's, not the daemon's. A paradigm moves between
them. A record-keeping strategy does not.

**And `statemachined.model` is the same pydantic in both**, and the same the
daemon validates with, so a graph is refused where you wrote it rather than
after a round trip. That is the reason this is one distribution: every place the
package could have been split leaves those models described twice, and a second
description is right until the day it is not.

```sh
make test-unit          # host-only: no daemon, no device, no socket
make test-integration   # against the firmware built for this machine
make test-e2e-local     # `statemachined serve` and `device`, two processes, a socket
make test-python        # all three
```

## Install it on a rig

`make packages` builds installable packages — the daemon and a vendored Python
under `/opt/braemons/statemachined`, a systemd unit, a udev rule that names the
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
The Python package, `python/src/`: [LGPLv3-or-later](python/LICENSE)**, so an
experiment importing it is not placed under copyleft — the same split, and the
same reason, as vstimd's client. The split is by *what is importable*, not by
directory: `python/tests/` is a test suite and stays GPL. That the importable
half now includes a client and a way to drive a board directly does not change
the line; it is the same argument reaching further. Every source file carries an
`SPDX-License-Identifier`.

The GPL here is a *choice*, not an inheritance: no Bpod source is copied,
translated or adapted anywhere in this repository — the references to it in the
code are comments comparing designs. We take the licence its authors chose
because we take their ideas. [`NOTICE`](NOTICE) records that, and the one piece
of third-party code in the tree (doctest, MIT, test-only).

Copyright © 2026 Joscha Schmiedt, University of Bremen.
