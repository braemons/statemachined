# statemachined — the daemon

> **Where this is going:** [`dev/DAEMON.md`](../dev/DAEMON.md) is the plan.
> `src/statemachined/` now holds three layers: `device/` is the wire, `model/`
> is a graph as a person authors it, and `graph_set_compiler.py` is the translation between
> them. `command_line_interface.py` is still the bench instrument this directory started as, and
> everything below is still true of it. `serve`, the supervisor, the API and the
> web UI arrive at M4e–M4g.

What exists today is the bench instrument [`dev/BRINGUP.md`](../dev/BRINGUP.md)
§4 and §5 ask for: one command out, one reply back, and the numbers M3 is
waiting on printed in a shape somebody can paste into
[`dev/HARDWARE.md`](../dev/HARDWARE.md).

**The bench instrument is not the daemon**, and the split survives the move as a
split between modules rather than between directories. `command_line_interface.py` sends one
command and prints what came back; it knows nothing about paradigms, trials or
graphs. Nothing that runs an experiment belongs in it.

The layers below it:

| | |
|---|---|
| `device/` | the wire, and only the wire. Framing, the session, the set upload, the result. Knows what an index is and never what a name is |
| `model/` | a graph, a line map and a record as a *person* writes and reads them. Pydantic, because this is the boundary where a file somebody edited arrives and "refuse it, naming the field" is the whole job. Knows nothing about messages |
| `graph_set_compiler.py` | the translation, and the only place that knows both. Names into indices, plus the check that the whole set fits the `caps` this board declared |

That split is what the daemon exists for: the wire speaks indices because the
device has 32 KB, a person speaks names, and neither should have to learn the
other's vocabulary.

```sh
make bringup ARGS="hello"                   # or, without the Makefile:
uv run --project daemon statemachined hello
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
tty, so the transport is one class (`serial_link.py`) and nothing above it knows which
it got. The part that is *not* transport-independent is fail-safe: BRINGUP.md §6
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
| `report` | `hello` + link load + `state`, printed as markdown for HARDWARE.md |
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
uv run --project daemon statemachined --hello state
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
uv run --project daemon statemachined serve --port 8081
```

The API is [`dev/API.md`](../dev/API.md), the generated schema is at
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
saved and loaded from the web UI. See `dev/API.md` §8.

## Tests that need no board

```sh
make test-integration   # builds the native device, then drives whole sessions
make test-daemon        # the same, skipping the integration half if unbuilt
make test-e2e           # the trial loop with a real triald at the other end
```

`tests/unit/` is arithmetic and translation: the framing, the compiler checked
message by message against `dev/PROTOCOL.md` §3.2, the clock's wrap.

`tests/integration/` drives whole sessions -- greet, upload, configure, start,
result, cancel races, link loss -- against `build/statemachined_native_device`,
which is `firmware/core`'s own session and engine built for this machine. The
far end is therefore not a mock: a mock answers what the test author believed
the protocol says, and this answers what the firmware says. The transport is a
real `socket://` URL through the daemon's own `serial_link.py`.

## The end-to-end suite, with triald

`make test-e2e` runs one whole trial across both daemons: triald picks the
trial and hands out its number, this daemon configures the firmware for that
number and runs it, and the outcome goes back to triald and lands in its record.
It is the only test of the *handover*, and the handover was broken with nothing
to catch it — this daemon sent `trial_id`, triald's schema forbade unknown
fields and had no such field, and every outcome came back 422 while
`tests/unit/test_triald_client.py` asserted the field against a mock that
answers 200 to anything. **A mock at the far end of a contract tests one side's
opinion of the contract twice.**

triald is a pip-installable FastAPI app, so it runs *in this process*: Starlette's
`TestClient` is an `httpx.Client` over it, and `TrialdClient` takes one. No
subprocess, no port, no teardown race — and what carries the bytes is the only
thing replaced.

It is a separate group and a separate target because triald is a private repo
and fetching it needs credentials. Without them the tests skip themselves and
say so, so `make test-daemon` on a fresh checkout never fails for want of
another repo. The contract itself is also checked without a device, by
`tests/unit/test_the_outcome_report_matches_trialds_schema.py`, which reads
triald's published OpenAPI and asserts what this daemon sends is something that
schema accepts.

See the contracts repo, `INTERACTIONS.md` §5.1 and §8.

## The hardware test suite

`tests/hardware/` is the automated half of BRINGUP.md §4 and §5 — everything
those sections ask a person to read off the screen, asserted instead.

```sh
make test-hardware                        # TARGET=... for a board elsewhere
make test-hardware ARGS="-k trial -v"     # ARGS goes straight to pytest
```

Connect a board and run it; there is no other setup. It greets the device once
— **which takes the rig** — and takes about 40 seconds. It is deliberately not
part of `make ci`, because a target that fails on every machine without a board
is a target people learn to ignore.

| File | What only a board can answer |
|---|---|
| `test_session_and_greeting.py` | `scan_hz` against the 10 kHz target; what the greeting declares |
| `test_framing.py` | a corrupt line, an over-long one, a resend — against real silicon and a real buffer |
| `test_trial.py` | a drawn 500 ms served to within 2 ms on the board's own clock; a result arriving whole from the ISR |
| `test_scan_health.py` | what a command costs the scan, as a regression test on the ISR handoff |
| `test_line_predicates.py` | predicates over several real pins — **needs three jumper wires** |

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
end on a timeout. Three jumper wires close that gap, by letting the board drive
its own inputs through a graph's entry actions:

| | | |
|---|---|---|
| **D10** → **D6** | output 0 → input 4 | |
| **D11** → **D7** | output 1 → input 5 | `all` over two lines, `any`, `none` |
| **D12** → **D8** | output 2 → input 6 | the rising-edge rule, and `level` |

Inputs 4–6 rather than 0–2 because BRINGUP.md §2 wires the bench switches as a
contact to **5 V**, and a jumper driving one of those pins would fight the switch
when it closed. As it stands the bench wiring and this harness share a board.

The wires are probed, not declared — a flag saying "the harness is attached"
would one day be passed against a board with a wire hanging loose, and the tests
would then read as though the firmware could not see its inputs. Without them,
those seven skip and name the wires.

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
