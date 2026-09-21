# statemachined — the Python side

Everything host-side in one package: the documents a paradigm is written in, two
ways to drive a board, and the daemon. Which tier you install says how you mean
to work.

```sh
pip install statemachined            # the documents, and talking to a daemon
pip install 'statemachined[device]'  # + open the serial port yourself
pip install 'statemachined[serve]'   # + be the daemon
```

| | tier | |
|---|---|---|
| `model/` | base | a graph, a line map, a config and a record as a *person* writes and reads them. Pydantic, because this is the boundary where a file somebody edited arrives and "refuse it, naming the field" is the whole job. Knows nothing about messages |
| `graph_set_compiler.py` | base | the translation, and the only place that knows both vocabularies. Names into indices, plus the check that the whole set fits the `caps` a board declared |
| `client/` | base | `StatemachinedClient`: the HTTP and WebSocket API in [`docs/reference/api.md`](../docs/reference/api.md), for when something else owns the board |
| `device/` | `[device]` | the wire, and `StatemachinedDevice` on top of it: open the port, greet, push the wiring, upload a set, run trials. Needs pyserial |
| `daemon/` | `[serve]` | the API, the web UI, the stores, the trace and the recordings. Needs fastapi and uvicorn |

That split is what the daemon exists for, and it survives being one package: the
wire speaks indices because the device has 32 KB, a person speaks names, and
neither should have to learn the other's vocabulary.

## One package, and why

Because every place it could have been cut leaves `model/` described twice.

A client that could not build a graph is half a client; one that carried its own
copy of `GraphDefinition` would carry a copy that is right until the day it is
not, and the day it is not is a session. So the models are here, the daemon
validates with them, the client sends them, and a bench script builds one — one
definition, and the daemon's own test suite is what proves it.

The dependency isolation that would have motivated a split is what the extras
are for. Something that only talks to a daemon installs pydantic, httpx and
websockets, and never a serial library or a web framework; an import that
reaches for a tier you have not installed says which one and how to get it,
rather than `No module named 'serial'`.

## The two classes, and why they are two

```python
from statemachined.client import StatemachinedClient   # something else owns the board
from statemachined.device import StatemachinedDevice   # you own the board
```

They share the trial loop, the graph set, the wiring, autorun and save, because
those are the *board's*. A paradigm moves between them unchanged.

They are not two backends behind one facade, because a daemon can do things a
direct connection cannot and the difference is not incidental: it keeps a
bounded trace ring, takes named recordings off that ring, holds a graph store
and saved configs on disk, and can tell you who else is watching. All of it
exists because the daemon **outlives the script that spoke to it**. A direct
connection has no ring to record from — your process was the only listener, and
what it did not keep is gone. One facade would have to answer
`recordings.start()` on both, and on one of them the answer would be a fiction.

> **Greeting a board takes the rig, and now two things can do it.** A board that
> was arming its own trials stops until it is told to again — deliberately, so a
> daemon that crashed cannot leave a board rewarding an animal nobody is
> watching. Which means `StatemachinedDevice` pointed at a board a daemon is
> holding is a second claimant on it. Over a tty the port is busy and you find
> out; over `socket://` you do not. Use the client for a board something else
> owns.

## Tests

Four tiers, and each adds exactly one thing the one below cannot say anything
about. None of them is a mock, and none needs a board.

```sh
make -C .. test-unit          # host-only: no daemon, no device, no socket
make -C .. test-integration   # against the firmware built for this machine
make -C .. test-runs          # whole sessions, both API paths, either far end
make -C .. test-e2e-local     # `statemachined serve` and `device`, two processes, a socket
make -C .. test-python        # all four
```

`tests/integration` drives whole sessions against `statemachined_native_device`
— the firmware's own session, parser, validator, scan loop and result chunker
built for the host — three ways over: through `StatemachinedDevice`, through the
daemon's API, and through the client in front of that daemon. `tests/e2e` starts
the two shipped commands as subprocesses and talks to them over a socket, which
is the only place uvicorn's WebSocket support is exercised at all: plain uvicorn
answers an upgrade with a 404 and every in-process test passes regardless.

`tests/runs` is the one that varies the *far end*. Every other tier is pinned to
one — `integration` and `e2e` to the host build, `hardware` to a board — so the
question a rig actually cares about had never been asked: does a session that
works against the host device work against a board, and does it work the same
through both ways of driving one? The same tests run through the daemon's HTTP
API and through `StatemachinedDevice`, against either far end:

```sh
make -C .. test-runs                              # the host build
make -C .. test-runs-hardware TARGET=/dev/ttyACM0 # the same tests, on a board
```

That is possible because the board presses its own levers — see the loopback
harness below, which exists in jumper wire and in software.

Build the native device first (`make -C .. integration-device`, which `make test`
does); without it every tier but the first skips itself and says how.

## The bench instrument

`command_line_interface.py` is still what [`docs/operations/bringup.md`](../docs/operations/bringup.md)
§4 and §5 ask for: one command out, one reply back, and the numbers M3 is
waiting on printed in a shape somebody can paste into
[`docs/operations/hardware.md`](../docs/operations/hardware.md). It knows nothing about paradigms,
trials or graphs, and nothing that runs an experiment belongs in it.

```sh
make bringup ARGS="hello"                   # or, without the Makefile:
uv run --project python statemachined hello
```

`uv` builds the environment on first use; nothing is installed into the system
interpreter, which is what keeps this clear of the Renode/Robot dependencies
the Makefile installs there.

## Where the device is

`-t/--target`, or `$STATEMACHINED_TARGET`, takes any of:

| | |
|---|---|
| `/dev/ttyACM0` | the reference board over USB CDC. The default |
| `192.168.1.40:5000` | a device on a network; shorthand for `socket://…` |
| `socket://host:5000`, `rfc2217://host:5000` | any pyserial URL, spelled out |

The protocol is lines of ASCII with a CRC, which is as true over TCP as over a
tty, so the transport is one class (`device/serial_link.py`) and nothing above it knows which
it got. The part that is *not* transport-independent is fail-safe: [`bringup.md`](../docs/operations/bringup.md) §6
turns on `hal::link_up()` going false when a USB port closes, and a device on a
switch has to decide for itself what a dead peer looks like — a missed `ping`,
most likely. That is a firmware question, not one this tool can answer.

The pin labels in `board_pin_labels.py` are keyed by the `board` string in `hello_ack`. An
unknown board prints bare line numbers rather than somebody else's pinout.

## Commands

| | |
|---|---|
| `hello` | opens a session and prints `scan_hz`, the number §4 is for. **Takes the rig**: a board arming its own trials stops |
| `state` | one `state_report`: `io.in` / `io.out` as bit rows, scan health, link counters |
| `watch` | polls `state` and prints `io` as it changes — hold a switch, watch `in` |
| `ping` | round trip and uptime |
| `load` | hammers the link, then reports whether `overruns` moved. Exits non-zero if it did |
| `report` | `hello` + link load + `state`, printed as markdown for [`hardware.md`](../docs/operations/hardware.md) |
| `raw` | a hand-written body without its closing brace; the CRC is appended here |
| `monitor` | reads and CRC-checks lines, **sending nothing** — the one way to watch the link without taking the rig |

Only `hello` and `report` greet the device. Nothing else does, on purpose:
greeting a board takes the rig from it, and a tool that ended somebody's
unattended session as a side effect of "just checking the state" would be a tool
nobody could safely point at a running box.

The device, however, refuses everything but `hello` before a session exists
(`not_ready`, context `hello`), so `state`, `watch`, `ping` and `load` need
`--hello` on a board that has not been greeted yet — which takes the rig. That
is the trade, and it is the operator's to make, not this tool's:

```sh
uv run --project python statemachined --hello state
```

`monitor` needs no session at all, since it sends nothing.

Nothing is ever retried. The protocol makes a blind resend safe, but a silent
retry would hide exactly the stall §5 is measuring.

## Graphs

[`graphs/`](../graphs) holds the worked examples: a go/no-go and a
two-alternative forced choice, plus the line map of the reference rig they are
authored against. They are meant to be read as much as run — they are what a
graph file *is*.

```sh
make test-daemon        # among other things, checks they still fit the board
```

A graph names states, lines and durations; nothing in it is an index, and
nothing in it is specific to a board. What makes it runnable on a *particular*
rig is the line map (which pin `lever_left` is) and that board's `caps` (whether
the set fits), and both of those meet the graph in `graph_set_compiler.py` rather than in
the file.

## Running it

```sh
uv run --project python statemachined serve --port 8081
```

The API is [`docs/reference/api.md`](../docs/reference/api.md), the generated schema is at
`/openapi.json`, and `/docs` is browsable. Everything else in this file is the
bench instrument; `serve` is the daemon, and the difference is that a bench
command borrows a board somebody is holding while a daemon takes it.

Configuration is in two files, and which is which is decided by whether the
daemon may write it. The **rig config** is
`/etc/braemons/statemachined-rig-config.toml` (`--config` to point elsewhere,
and its absence means the built-in defaults, which is what a bench run wants):
the device, the expected board, the directories, where triald is. Hand-edited,
and never written back. A **state-machine config** is the line map and the
graphs, one file per config under `/var/lib/braemons/statemachined/configs/`,
saved and loaded from the web UI. See `docs/reference/api.md` §8.

## Tests that need no board

```sh
make test-integration   # builds the native device, then drives whole sessions
make test-daemon        # the same, skipping the integration half if unbuilt
```

`tests/unit/` is arithmetic and translation: the framing, the compiler checked
message by message against `docs/reference/protocol.md` §3.2, the clock's wrap.

`tests/integration/` drives whole sessions -- greet, upload, configure, start,
result, cancel races, link loss -- against `build/statemachined_native_device`,
which is `firmware/core`'s own session and engine built for this machine. The
far end is therefore not a mock: a mock answers what the test author believed
the protocol says, and this answers what the firmware says. The transport is a
real `socket://` URL through the daemon's own `device/serial_link.py`.

## This daemon reports to nobody

There is no setting here naming another daemon and no outbound call of any kind.
Every trial's result goes into the trace, and whatever wants it — triald, a
console, a browser tab, a script — opens `WS /api/trace/stream` and reads.
Opening the socket is the whole of subscribing; closing it is the whole of
leaving. Nothing is registered, nothing is retried, and a rig with nobody
watching runs and records trials exactly the same.

That is not indifference, it is the only honest position: this daemon cannot
know whether a consumer exists, or should, or is running a session. **Only a
consumer can tell "not yet" from "never"**, so the deadline on a missing outcome
belongs to whoever is waiting for one.

`GET /api/observers` and the **Observers** panel say who is reading right now.
It is a debugging aid and not a contract — the daemon never acts on that list —
and it exists for one question: when trials stop reaching triald, is nothing
connected, or is something connected and receiving nothing? Without it that is
answered with a packet capture. `?observer=<name>` on the stream URL is a label
for that screen, self-declared and unverified, because nothing is granted by it.

The stream loses nothing. A consumer too slow for the ring is told exactly which
entries are gone and disconnected, rather than handed a shorter answer that
looks complete — and it can fetch any trial it missed with
`GET /api/trace/trial/{id}`.

## The handover to triald, and where it is tested

One whole trial across both daemons — triald picks the trial and hands out its
number, arms this daemon for exactly that number, starts it, watches the trace
go by, pulls that trial's events by id and turns them into an outcome in its own
record — is tested in the **contracts** repo, `rig/`, alongside the tests that
run all three daemons. `make rig-local` there runs it against local checkouts.

It used to run from here, and the argument for that was the expensive fixture:
the firmware built for this machine is in this repository. The argument against
it turned out to be larger. A test of a handover is not a test of this daemon,
and paying for it here meant a dependency group installing triald, a lockfile
pin on triald's main branch, and a CI job fetching another repository — for a
suite that could then only ever run against whichever triald commit the lock had
captured.

Nothing in this package imports triald, has a setting naming it, or makes any
outbound call at all. That was always true of the code; it is now true of the
build as well.

See the contracts repo, `INTERACTIONS.md` §5.1 and §8.

## The hardware test suite

`tests/hardware/` is the automated half of [`bringup.md`](../docs/operations/bringup.md) §4 and §5 — everything
those sections ask a person to read off the screen, asserted instead.

```sh
make test-hardware                        # TARGET=... for a board elsewhere
make test-hardware ARGS="-k trial -v"     # ARGS goes straight to pytest
```

Connect a board and run it; there is no other setup. It greets the device once
— **which takes the rig** — and takes about 90 seconds. It is deliberately not
part of `make ci`, because a target that fails on every machine without a board
is a target people learn to ignore.

| File | What only a board can answer |
|---|---|
| `test_session_and_greeting.py` | `scan_hz` against the 10 kHz target; what the greeting declares |
| `test_framing_errors.py` | a corrupt line, an over-long one, a resend — against real silicon and a real buffer |
| `test_trial_lifecycle.py` | a drawn 500 ms served to within 2 ms on the board's own clock; a result arriving whole from the ISR |
| `test_scan_health.py` | what a command costs the scan, as a regression test on the ISR handoff |
| `test_line_predicates.py` | predicates over several real pins — **needs the jumper wires below** |
| `test_timing_accuracy.py` | duration accuracy against the drawn value, and pin-to-transition latency — **needs the wires** |

The graph upload and the result reassembly this suite needs used to live in
`hardware_test_harness.py`, with a note that they were the bridge's job and should move when
`bridge/` existed. They have: they are `device/graph_set_upload.py` and `device/trial_result_reassembly.py`,
and the suite imports them. So it now tests the daemon's own codec against real
hardware rather than a copy of it, which is the whole reason M4a went first.
What is left in `hardware_test_harness.py` is what is genuinely test-only — raw-line access,
deliberate refusals, and the enums a bench instrument has no use for.

### The loopback harness

Nothing in this repository drives a *predicate* from real pins. The host suite
covers the three masks thoroughly and Renode checks that an input pin arrives as
the right line number, but both hardware trials — Renode's and this suite's —
end on a timeout. Eight jumper wires close that gap, by letting the board drive
its own inputs through a graph's entry actions: **output line _n_ to input line
_(n + 4) mod 8_.**

| Wire | | | Wire | | |
|---|---|---|---|---|---|
| 1 | **D10** → **D6** | output 0 → input 4 | 5 | **A1** → **D2** | output 4 → input 0 |
| 2 | **D11** → **D7** | output 1 → input 5 | 6 | **A2** → **D3** | output 5 → input 1 |
| 3 | **D12** → **D8** | output 2 → input 6 | 7 | **A3** → **D4** | output 6 → input 2 |
| 4 | **A0** → **D9** | output 3 → input 7 | 8 | **A4** → **D5** | output 7 → input 3 |

**Why the shift and not output _n_ to input _n_.** A straight-through harness
cannot tell a correct board from one whose reported input word is secretly the
output word: raise output 0, see bit 0 set, pass. Under the shift each output
has a unique and non-obvious expected input bit, so that failure — and any
rotation or off-by-one in either pin table — fails rather than passing for the
wrong reason. It also covers all sixteen lines, where the three-wire harness
this replaced left thirteen pins never once proven to be the pin the table
claims.

**It replaces the bench switches rather than sitting beside them.** Wires 5 and
6 land on D2 and D3, which [`bringup.md`](../docs/operations/bringup.md) §2
wires as a contact to **5 V**, and an output driving low against a closed switch
is a short. Take the switches off while the harness is on, or put **1 kΩ in
series** in those two wires. Sharing the output pins with the bench lamps is
harmless.

The wires are probed, not declared — a flag saying "the harness is attached"
would one day be passed against a board with a wire hanging loose, and the tests
would then read as though the firmware could not see its inputs. The probe names
the wires actually missing, since seven-of-eight is the normal way this fails.

**The same harness exists in software.** `hal::set_native_loopback()` gives the
host build the identical rule, switched on with `STATEMACHINED_LOOPBACK=8`, so
`tests/runs/` runs unchanged with a board and without one. It reproduces which
line a level arrives on and that it arrives a scan later; it reproduces nothing
about timing, and no timing assertion is made against it.

## Framing

`device/message_framing.py` is the **third** implementation of the framing in this
repository, after the firmware's and the emulator tests'. Until M4a it was not:
it imported `emulation/tests/statemachined_protocol.py`, on the grounds that a
third copy would not add a third opinion, only somewhere for the rules to drift.

An installed package cannot do that — a `.deb` has no `emulation/` directory —
so the rules are written out, and `tests/unit/test_message_framing.py` is what replaces the
guarantee: a fixed set of lines with known CRCs, checked against both Python
implementations in one run, and against a third set of bytes generated on the
spot so the agreement is not just about five memorised lines. The CRCs in
`wire_vectors.json` were verified against `firmware/core/protocol/crc16.cpp`, so
they are the device's arithmetic rather than a host module's.

```sh
make test-daemon        # host-only; part of `make ci`
```
