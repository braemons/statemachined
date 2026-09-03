# statemachined-bringup

The bench instrument [`dev/BRINGUP.md`](../../dev/BRINGUP.md) §4 and §5 ask for:
one command out, one reply back, and the numbers M3 is waiting on printed in a
shape somebody can paste into [`dev/HARDWARE.md`](../../dev/HARDWARE.md).

It is **not the bridge.** It knows nothing about paradigms, trials or graphs,
and anything here that starts to look like it is running an experiment belongs
in `bridge/` instead.

```sh
make bringup ARGS="hello"                   # or, without the Makefile:
uv run --project tools/bringup statemachined-bringup hello
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
tty, so the transport is one class (`link.py`) and nothing above it knows which
it got. The part that is *not* transport-independent is fail-safe: BRINGUP.md §6
turns on `hal::link_up()` going false when a USB port closes, and a device on a
switch has to decide for itself what a dead peer looks like — a missed `ping`,
most likely. That is a firmware question, not one this tool can answer.

The pin labels in `board.py` are keyed by the `board` string in `hello_ack`. An
unknown board prints bare line numbers rather than somebody else's pinout.

## Commands

| | |
|---|---|
| `hello` | opens a session and prints `scan_hz`, the number §4 is for. **Ends demo mode until the next reset** |
| `state` | one `state_report`: `io.in` / `io.out` as bit rows, scan health, link counters |
| `watch` | polls `state` and prints `io` as it changes — hold a switch, watch `in` |
| `ping` | round trip and uptime |
| `load` | hammers the link, then reports whether `overruns` moved. Exits non-zero if it did |
| `report` | `hello` + link load + `state`, printed as markdown for HARDWARE.md |
| `raw` | a hand-written body without its closing brace; the CRC is appended here |
| `monitor` | reads and CRC-checks lines, **sending nothing** — the one way to watch the link without ending demo mode |

Only `hello` and `report` greet the device. Nothing else does, on purpose: a
tool that ended demo mode as a side effect of "just checking the state" would
blank the lamps somebody was watching.

The device, however, refuses everything but `hello` before a session exists
(`not_ready`, context `hello`), so `state`, `watch`, `ping` and `load` need
`--hello` on a board that has not been greeted yet — which ends demo mode. That
is the trade, and it is the operator's to make, not this tool's:

```sh
uv run --project tools/bringup statemachined-bringup --hello state
```

`monitor` needs no session at all, since it sends nothing.

Nothing is ever retried. The protocol makes a blind resend safe, but a silent
retry would hide exactly the stall §5 is measuring.

## The hardware test suite

`tests/hardware/` is the automated half of BRINGUP.md §4 and §5 — everything
those sections ask a person to read off the screen, asserted instead.

```sh
make test-hardware                        # TARGET=... for a board elsewhere
make test-hardware ARGS="-k trial -v"     # ARGS goes straight to pytest
```

Connect a board and run it; there is no other setup. It greets the device once
— **which ends demo mode** — and takes about 40 seconds. It is deliberately not
part of `make ci`, because a target that fails on every machine without a board
is a target people learn to ignore.

| File | What only a board can answer |
|---|---|
| `test_session.py` | `scan_hz` against the 10 kHz target; what the greeting declares |
| `test_framing.py` | a corrupt line, an over-long one, a resend — against real silicon and a real buffer |
| `test_trial.py` | a drawn 500 ms served to within 2 ms on the board's own clock; a result arriving whole from the ISR |
| `test_scan_health.py` | what a command costs the scan, as a regression test on the ISR handoff |
| `test_lines.py` | predicates over several real pins — **needs three jumper wires** |

The tests share `Session` and its framing with the CLI, and add two things in
`harness.py` that a bench instrument has no business having: a graph upload and
a result reassembled against its rolling checksum. Both are the *bridge's* job
by the rule at the top of this file, and both should move there when `bridge/`
exists — at which point this suite tests the bridge's codec against real
hardware, which is strictly better than testing a copy of it.

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

Inputs 4–6 rather than 0–2 because BRINGUP.md §2 wires the demo's switches as a
contact to **5 V**, and a jumper driving one of those pins would fight the switch
when it closed. As it stands the demo wiring and this harness share a board.

The wires are probed, not declared — a flag saying "the harness is attached"
would one day be passed against a board with a wire hanging loose, and the tests
would then read as though the firmware could not see its inputs. Without them,
those seven skip and name the wires.

## Framing

Shared with the emulator tests (`emulation/tests/statemachined_protocol.py`)
rather than written a third time. There are deliberately two implementations of
the protocol — the firmware's and the tests' — and a third would not add a third
opinion, only somewhere for the rules to drift. That is why this has to be run
from a checkout.
