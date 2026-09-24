# The Rust port: where it is, and what is left

Companion to [`RUST_PORT.md`](RUST_PORT.md), which is the plan. This is the
state of the work.

**The Rust daemon is the daemon now.** The Python one is gone from the
repository, and so is every Python module that was not the client; the package
is the Rust binary at `/usr/bin/statemachined`. It has still not run a session
on a rig (open question 1).

---

## The board link is protobuf, and the Python is retired

The firmware and `daemon-rs` talk COBS-framed protobuf with a CRC-16, as
mousewheeld does: `proto/statemachined/link/v1/link.proto`, nanopb on the
board, the descriptor in the daemon. The Python daemon was retired rather than
moved to it, and with it went everything in Python but the client:

* **The differential comparison is over.** `tools/compare_daemons.py` last
  reported 170 of 170 against the NDJSON wire. What still holds the Rust daemon
  to Python is the recorded corpora in `daemon-rs/tests/` — the compiler's
  messages, the documents' verdicts, the line maps, the clock — which are
  fixtures now, and the upload test, which checks that what a board decodes is
  the message Python uploaded for every compiled case.
* **Board tests go through the client.** `client/python/tests/hardware/` is the
  hardware suite, driving the board through the daemon; `make test-hardware
  TARGET=native` runs it against the firmware built for this machine, in CI.
* **The native device listens on its own port.** `statemachined device` and its
  bridge are gone; `build/statemachined_native_device --port N` is the far end
  for tests and a bench, and it is not packaged.
* **What was lost:** the Renode Robot suite, which covered the HAL under
  emulation (`emulation/README.md` says how it could come back through the
  client), and the tests of the NDJSON framing, which have nothing left to test.

## Done

**All 50 rpcs.** Every one has a body, and every one is held to the Python
daemon's answer by `tools/compare_daemons.py`. That is not the cutover: see
*Left*.

| | |
|---|---|
| `Configuration` | all three: `ReadHealth`, `ReadConfiguration`, `PatchConfiguration` |
| `GraphStore` | all seven: `ListGraphs`, `ReadGraphFile`, `WriteGraphFile`, `DeleteGraph`, `ValidateGraph`, `ValidateGraphFile`, `UploadGraph` |
| `StateMachineConfigStore` | all five: `ListConfigs`, `ReadConfigFile`, `WriteConfigFile`, `DeleteConfig`, `LoadConfig` |
| `Device` | all ten: `ReadDevice`, `OpenLink`, `ReadLines`, `WriteLineMapFile`, `ReadFirmware`, `ReadSerialMonitor`, `WatchSerialMonitor`, `ReadAutorun`, `WriteAutorun`, `SaveSettings` |
| `Recording` | all nine |
| `State` | all six: `ReadState`, `WatchState`, `ReadTrace`, `WatchTrace`, `ReadTrialTrace`, `ReadObservers` |
| `Trial` | all four: `Configure`, `Start`, `Cancel`, `ReadResult` |
| `Session` | all six: `ReadSession`, `Open`, `UploadGraphs`, `Close`, `SetActiveGraph`, `ClearActiveGraph` |

### Modules

| Ported | From | Lines (py) |
|---|---|---|
| `model/graph_definition.rs` | `model/graph_definition.py` | 560 |
| `model/line_map.rs` | `model/line_map.py` | 327 |
| `model/state_machine_config.rs` | `model/state_machine_config.py` | 99 |
| `model/trial_outcome.rs` | `model/trial_outcome.py` | 101 |
| `rig_configuration.rs` | `daemon/rig_configuration.py` | ~130 |
| `store.rs` | `daemon/graph_store.py` + `state_machine_config_store.py` | ~180 |
| `device/message_framing.rs` | `device/message_framing.py` | 145 |
| `device/message_vocabulary.rs` | `device/message_vocabulary.py` | 134 |
| `device/serial_link.rs` | `device/serial_link.py` | 157 |
| `device/request_response_session.rs` | `device/request_response_session.py` | 170 |
| `device/device_clock_correlation.rs` | `device/device_clock_correlation.py` | 188 |
| `device/device_pin_map.rs` | `device/device_pin_map.py` | 71 |
| `device/board_pin_labels.rs` | `device/board_pin_labels.py` | 44 |
| `device/statemachined_device.rs` | `device/statemachined_device.py` — all but `set_enabled_timers`, which no rpc calls | 848 (most) |
| `device/trial_result_reassembly.rs` | `device/trial_result_reassembly.py` — the collector; the bench's blocking reader stays in Python | 161 (most) |
| `model/trial_record.rs` | `model/trial_record.py` | 138 |
| `grpc/trial.rs` | `RigService`: the trial, the visit and result records, the link thread | ~250 of 817 |
| `graph_set_compiler.rs` | `graph_set_compiler.py` | 636 |
| `device/state_visit_trace.rs` | `device/state_visit_trace.py` | 206 |
| `observer_registry.rs` | `daemon/observer_registry.py` | 163 |
| `firmware_manifest.rs` | `daemon/firmware_manifest.py` | 55 |
| `device/device_line_monitor.rs` | `device/device_line_monitor.py` | 112 |
| `event_recording.rs` | `daemon/event_recording.py` | 431 |
| `mdns_service_advertisement.rs` | `daemon/mdns_service_advertisement.py` — `mdns-sd` where Python uses `zeroconf`; the rig identifier hashed identically | 174 |
| `device/graph_set_upload.rs` | `device/graph_set_upload.py` — **the daemon's half**; the hand-built uploaders stay with the hardware suite | 206 (part) |

### What the port is held to

Each piece is checked against something that is not "it compiles", and each
check was **confirmed by breaking the implementation and watching it fail**. A
green differential test that cannot go red is not evidence.

| Piece | Held to | Cases |
|---|---|---|
| Documents | pydantic's verdict, generated by `tools/document_corpus.py` | 147 |
| Framing | `daemon/tests/unit/wire_vectors.json`, golden against `firmware/core/protocol/crc16.cpp` | 12 tests |
| Transport | a real TCP socket, bytes cut in the wrong places | 9 tests |
| Session | a fake board made to interleave events with replies | 9 tests |
| Clock | Python's answers over a swept input space, `tools/clock_trace.py` | 39 operations |
| Line map + wiring | Python's answers, `tools/line_map_cases.py` | 31 cases |
| Stores | both daemons on identical stores, driven by one browser bundle | 17 calls |
| Connection | `build/statemachined_native_device` — the real firmware on a socket | driven live |
| Compiler | every upload message and read-back table Python produces, `tools/graph_set_cases.py`; refusals by sentence | 39 cases, 495 messages |
| Trace | `test_state_visit_trace.py`, ported one test for one, and the day's file line against Python's `json.dumps` | 11 tests |
| The cutover's acceptance test | `contracts/e2e-tests/`, unmodified, with `make e2e-rust` putting `tools/e2e_rust/statemachined` first on PATH so `serve` starts the Rust daemon — vstimd, triald and the native device as they are | 24 passed, 5 skipped (need a board), 1 xfail — the same as against Python |
| Everything that answers | `tools/compare_daemons.py`: both daemons as processes, each on its own native device, identical stores, driven by the real Python client; answers and refusals compared whole, trailer included; three startups — nothing, a config and a board, a config nobody stored — and a session of trials on one seed, so every draw must agree; every line a daemon sends at startup, byte for byte; a recording across a pause, with its gap; the device pulled out mid-trial and plugged back in | 170 calls |
| Recordings | `test_event_recording.py`, ported one test for one, and the manifest's shape against Python's `json.dumps` | 15 tests |
| Upload | every framed line Python sends, byte for byte, rolling checksum included; then `set_ok` from the native firmware for every set that fits it | 20 sets |

### Bugs this found in the Python daemon

A port is a second implementation, and a second implementation is what finds
these. All nine are fixed in `daemon/`, each in its own commit, each with a
regression test:

1. **`ReadGraphFile` served a file it would not take back.** `exclude_defaults`
   dropped the discriminator off every distribution, so a graph downloaded
   through the API failed to re-upload with `union_tag_not_found` — on a field
   nobody had touched. Nothing caught it because every test read the *model*
   back, never the file the API serves.
2. **A timestamp within half a microsecond of a second was not a timestamp.**
   `round()` reached 1_000_000 and six-digit formatting wrote seven:
   `2023-11-14T22:13:20.1000000Z`, on a second also wrong by one. Every trace
   line goes through it.

### And two more in the Python daemon

3. **The same timestamp bug, three more times.** `state_visit_trace`,
   `event_recording` and `device_line_monitor` each carried their own copy of
   the formatter fixed in (2), unfixed. Every `recorded_host_time` in the
   trace went through one.
4. **A startup config that did not load said why in a repr**: the reason
   arrived in `ReadDevice` wrapped in double quotes, because the store's
   "not stored" is a `KeyError`.

### And five more in the Python daemon, from the later rpcs

5. **A patched seed or baud never reached the device.** Only the target and
   the expected board were passed on; `PATCHABLE` promised the seed "takes
   effect on the next connection", and it did not.
6. **`graph_mode` took any string.** The model's `Literal` is not checked on
   assignment, so "banana" was kept and reported back as the rig's mode.
7. **A link the daemon closed itself was written down as lost.** The link
   thread checked for a connection outside the lock; a re-greeting that found
   the wrong board closed it in between, and the next pump's
   `DeviceNotConnected` became a `link_lost` entry. The Rust thread had the
   same shape and now asks again under the lock too.
8. **`ReadAutorun` never said which graph.** It read `slot` and `graph` from a
   reply that carries `graph_index` and no name, so every answer was slot 0 and
   no graph. The name now comes from the committed set, as the proto says. The
   seed is still always zero: the board does not report it.
9. **A recording name ending in a newline was taken.** `NAME_PATTERN` was
   applied with `re.match`, whose `$` also matches before a trailing newline.

### And in the firmware

**A second set with a global timer was refused.** `GraphSet::assign` copied
every pool but the timers, and `set_begin` clears the live set by assigning an
empty one — so the timer count outlived it, and the next set's first
`graph_timer` was `bad_order`. The Python daemon hit it identically; nothing
had uploaded two timer sets to one device before. Fixed in
`firmware/core/graph/graph_set.cpp`, with a test for the clear and one for the
copy.

**Not fixed: a saved set loses its timers.** `io/settings_store.cpp` writes no
timers at all, so a set restored after a restart (`save`, autorun) comes back
without them. The fix changes the flash record's layout, so it is its own
change, with a record version.

### And in the port itself

**The heartbeat never fed the clock.** The Rust ping read the pong's `t_us`,
which the firmware does not send; the device clock is `us` (protocol.md 4.5).
So no visit could ever have had a host time. Found porting the visit stream,
which is the first thing to use the correlation.

**A board that went away read as a quiet one.** `SerialLink` took end of file
for "no line yet", so a closed socket or an unplugged tty looked like a healthy
board with nothing to say; the daemon sat on the dead link until a request
timed out, and no `link_lost` was ever written. It raises now, in pyserial's
words.

**`DeleteGraph` would delete a graph the board was running.** Python refuses
with `graph_in_use` while the committed set holds it; the port had the delete
and not the guard. Harmless until there was a committed set to guard.

**`graph_named`'s refusal quoted the name with Rust's `{:?}`**, double quotes
where Python's `!r` gives single. The compiler's cases never reached it; the
trials run did, and the compiler test now holds the whole sentence.

**Found by starting both daemons the ways a rig starts them**, which the
comparison did not do until the trace needed it:

* `startup_state_machine_config` and `connect_on_startup` were read and
  ignored: the Rust daemon started with nothing loaded and no board. Now
  `DaemonState::start` does what `RigService.start` does, before the server
  answers.
* `ReadDevice` answered from the greeting alone, on the argument that a
  status call should not touch the board. Python reads a state report, and
  the result was `has_wiring: false` after the wiring was pushed, no link or
  scan health, and three capacity fields at zero. Ported as Python has it;
  whether a status call should touch the board is a question for both.
* `ReadLines` never set `is_high_now`, and put the board's pin names in
  `pin_label` where Python keeps the config's. Both ported as Python has them.
* An observer's address was empty: axum owns the socket, and hands tonic the
  peer only when served `with_connect_info`.

**Refusals had no trailer.** Python sends every refusal's
`statemachined.v1.Error` in `statemachined-error-bin`, and the Python client
tells "the daemon has no board" from "nothing answered" by whether it is
there. The Rust daemon sent the status alone, so a client read every Rust
`unavailable` as the daemon being down. `grpc/refusal.rs` ports
`refusals.py`, and the rpcs ported before it now use it too. It also moved a
device's own refusal from `unavailable` — "retry unchanged" — to Python's
`failed_precondition` carrying the device's code.

**The codes are Python's, including two that are arguable:** a board that
does not answer in time, and a board that is not the one the rig config names,
are `internal` in Python because its table has no rule for either. Ported as
they are; worth changing in both daemons afterwards.

The trace entries Python writes around these calls (`config_loaded`,
`session_opened`, `graph_set_uploaded`) are not written yet: the trace is not
ported. They arrive with it.

The compiler's cases reach `LineMap`'s lookups, which the line-map cases did
not, and found `line_map.rs` answering two refusals differently from Python:

1. **An unresolved line was reported as a missing one.** A line configured by
   `pin_label` alone, before the board was asked, said "no input line is called
   'abort'" — which sends somebody renaming a line that exists — where Python
   says it has not been resolved against a board yet.
2. **"This rig has:" listed lines in config order**, where Python sorts them.

---

## Left

**Not exercised by any test:** a gap in the visit stream's `seq` (the
`sequence_gap` entry), which needs a board that drops a frame on purpose.
`reconnect_and_restore` and `wait_for_line_start` are not reachable through any
rpc.

**Outside this repository:** `contracts/e2e-tests` is changed to match, in its
working tree and not yet committed, because it sits on uncommitted work of its
own that it depends on. `make test-local` builds this daemon and the native
device and puts them first on PATH (24 passed, 5 skipped, 1 xfailed); the
fixtures run `statemachined_native_device --port 0`; the container takes
`/usr/bin/statemachined` and the native device from the release's
`statemachined-native-device-amd64` asset; `run_on_hardware.py` probes a board
through the daemon and `statemachinectl`. Its `make test` stays red until a
statemachined release with the Rust package and that asset is pinned.

---

## Open questions

1. **A rig running this for a real session.** The Python daemon is retired
   without one, so the cutover has no safety net beyond the test suites. This is
   about lab time, not code, and it is the question that decides whether the
   plan is safe (`RUST_PORT.md` §8.1).
2. **The `/api` mDNS TXT record is vestigial.** statemachined writes it,
   `console/discovery.mjs:117` passes it through with a default, and nothing
   reads it. Safe to drop, but it crosses two repositories.
3. **Leaked `statemachined_native_device` processes** came from the Python
   bridge, which left its device running whenever it was killed rather than
   stopped. The bridge is gone; the tests that start the device now kill it.
4. **The second port.** The Rust daemon answers on `--port + 1` as well, so no
   client changes at the cutover. Dropping it later means moving
   `statemachined-client`'s default, triald's executor setting and the e2e
   fixtures' arithmetic together, in `contracts/DAEMON_LAYOUT.md`.
5. **`OpenLink` does not restore a board's set.** After a board resets, both
   daemons re-greet and keep believing in the set they uploaded; the board
   refuses the next trial until a session is opened again.
   `reconnect_and_restore` exists for exactly this and nothing calls it — in
   either daemon. Ported as it is; worth deciding whether `OpenLink` (or the
   link thread, on a lost link) should use it.
6. **One default differs on purpose.** With no `--host` the Rust daemon binds
   loopback where Python bound every interface; the packaged unit says
   `--host 0.0.0.0`.
