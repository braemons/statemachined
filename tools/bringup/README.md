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

## Framing

Shared with the emulator tests (`emulation/tests/statemachined_protocol.py`)
rather than written a third time. There are deliberately two implementations of
the protocol — the firmware's and the tests' — and a third would not add a third
opinion, only somewhere for the rules to drift. That is why this has to be run
from a checkout.
