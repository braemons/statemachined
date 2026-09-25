# Plan: statemachined and triald in Rust

> **Status:** Approved for statemachined, 2026-09-21. Port in progress —
> see [`RUST_PORT_STATUS.md`](RUST_PORT_STATUS.md) for where it has got to.
> triald follows (§5).
> **Depends on:** `MIGRATION_PLAN.md` landing first. That is not sequencing
> preference — see §2.
> **Reference:** mousewheeld, which already is what these two would become.

---

## 1. Why

Not "Rust is faster." These daemons wait on a serial port and a browser; nothing
here is compute-bound. The reasons are structural, and each one is a thing that
exists today only because the daemons are Python:

| Today | After |
|---|---|
| `api/web_edge.py` — ~270 lines implementing gRPC-Web by hand, in each repo | One line: `tonic_web::GrpcWebLayer`. **The file is deleted, not ported.** |
| Two ports per daemon (`grpc_port_for` = `port + 1`) | One. tonic is a tower service; it merges into the axum router |
| Two listeners in one `asyncio.run`, `uvicorn.Server.serve()` beside `grpc.aio` | One `axum::serve` |
| `EdgeContext`, a hand-made stand-in for `grpc.aio.ServicerContext` — and the class of bug it caused | Gone. There is one context type because there is one server |
| A packaging check that must prove uvicorn, grpcio and pydantic all arrived | `cargo build` |

The `EdgeContext` bug is the one worth naming, because it is the shape of what
this removes rather than a one-off. `observer_registration.watching` began
calling `invocation_metadata()` and `peer()`; the stand-in had neither; **every
stream a browser opened died with an `AttributeError` while every integration
test passed**, because every integration test used the other transport. That
failure is only possible when two transports are implemented separately. In
mousewheeld there is one.

**What this is not:** a rewrite of the interesting code. The graph compiler, the
trial loop, the policy and adaptive logic are the value of these daemons and
they are ported, not reconsidered. Anything that looks like an improvement
during the port is a separate change, made afterwards, in Python or Rust,
visible in its own diff.

---

## 2. Why the wire must be fixed first

`MIGRATION_PLAN.md` puts both Python daemons on the same gRPC-Web wire
mousewheeld already speaks. **That is what makes this a server-side
reimplementation rather than a rewrite of everything at once.**

After it lands, all of this is fixed and does not change here:

- the `.proto` files — the interface, and the acceptance criteria
- `client/web/` — the panels and the browser client, byte for byte
- `client/python/` — what experimenters' scripts import
- the `/elements/` contract a console fetches by URL
- `contracts/e2e-tests/` — which becomes the acceptance test for the rewrite

So the rewrite changes one thing: **what answers the socket.** Everything that
talks to it is a control, and a regression shows up as a test that passed
yesterday.

Done in the other order — rewriting while the wire, the client and the language
all move — there is nothing to bisect against.

---

## 3. Size, measured

| | statemachined | triald | mousewheeld *(done, in Rust)* |
|---|---|---|---|
| daemon source | 14,004 py | 10,062 py | **9,751 rs** |
| of which generated protobuf | 3,369 | 2,277 | (build-time, not in tree) |
| **hand-written** | **10,635** | **7,785** | 9,751 |
| tests | 10,973 | 4,639 | — |

**mousewheeld is the estimate.** It is the same shape of thing — a board over a
serial link, typed gRPC services, a model with a compiler, panels on one port,
a publisher — and it is 9,751 lines. statemachined's comparable surface
(`daemon/` 5,260 + `device/` 2,840 + `model/` 1,238 = 9,338) lands within a few
hundred lines of it. That is a real calibration, not a guess.

What it does **not** tell us: mousewheeld was written in Rust from the start.
Porting carries work greenfield does not — reading intent out of Python,
reproducing file formats exactly, and keeping a test suite meaningful across a
language boundary.

---

## 4. The three hard parts

Everything else is typing. These are where the risk is.

### 4.1 The device layer — statemachined only

`device/` is 2,840 lines and it is the part that touches hardware:
`message_framing.py`, `message_vocabulary.py`, `serial_link.py`,
`request_response_session.py`, `device_clock_correlation.py`,
`trial_result_reassembly.py`, `graph_set_upload.py`.

**mousewheeld's `link/` and `device/` are the closest thing to a template this
job has** — same problem, same family, already solved in Rust, including the
serial framing and the partial-read case its tests pin down.

The genuinely hard one is `device_clock_correlation.py`: mapping device time to
host time is the thing every timestamp in a recording depends on. It must be
ported as *the same arithmetic*, and checked against recordings made by the
Python daemon rather than against its own idea of correct.

`native_device_on_a_socket.py` — the firmware built for the host, driven over
TCP — is what lets most of this be tested without a board, and it keeps working
because the firmware is C and is not in scope.

### 4.2 The documents

`model/` has four pydantic models that are **user data on rigs**:
`graph_definition.py`, `state_machine_config.py`, `line_map.py`,
`trial_record.py`.

A graph file somebody wrote last year must parse identically, and a refusal
must still name the field. serde is the tool; the risk is not the tool.

**The check that makes this safe:** every document on every rig, and every one
in the test corpus, parses under both implementations to the same structure —
and every file that is *refused* is refused by both, with the same code and the
same `context` field. Refusals are half the contract and the half that is easy
to forget: `graph_set_compiler.py` (167+ lines) exists to say *what overflowed*,
not just "no".

**Done, 2026-09-21 — and it did not change the plan.** `graph_definition.py`
(560 lines) is ported; `tools/document_corpus.py` generates the three real
graphs plus one systematic mutation per refusal rule, with pydantic's verdict on
each, and `daemon/tests/documents.rs` holds the Rust implementation to it.
**105/105 agree**: 3 accepted and identical field-by-field after a round trip,
102 refused by both.

The harness was checked by breaking it — disabling the unreachable-state rule
fails three cases by name, removing `deny_unknown_fields` fails three more — so
the green is evidence rather than an absence of evidence.

Not compared: the wording of a refusal. serde's parse errors are not pydantic's,
and holding two libraries to one sentence is a test about string formatting.

**`state_machine_config.py` (99) and `line_map.py` (327) followed**, with 42
config documents added to the corpus — 147/147 agree. `trial_record.py` (138) is
the one left, and it waits on the device layer that produces one.

**The method earns its keep.** Comparing the two daemons over the wire, on
identical stores, found four disagreements — one of which was a real bug in the
*Python* daemon: `ReadGraphFile` served a file it would not take back, because
`exclude_defaults` dropped the discriminator off every distribution. A port is
a second implementation, and a second implementation is what finds these.

### 4.3 The tests

15,612 lines of Python tests across the two repos. They do not port
mechanically, and treating them as a chore is how the safety net is lost.

Three kinds, each handled differently:

- **The e2e suite** (`contracts/e2e-tests/`) — language-agnostic, drives the
  daemons over gRPC. **Keeps working unchanged. It is the acceptance test.**
- **Unit tests of pure logic** (the compiler, policy, selection, outcomes) —
  port them. They encode decisions nobody wrote down elsewhere.
- **Tests of Python-specific structure** (`test_the_browser_edge.py`,
  `test_web_user_interface_assets.py`) — **delete.** They test a thing that no
  longer exists. `test_the_browser_edge.py` in particular exists to hold two
  transports together; in Rust there is one.

---

## 5. Order

| # | Step | Why here |
|---|---|---|
| 0 | **`MIGRATION_PLAN.md` lands** | §2. The wire stops moving |
| 1 | ~~**Document round-trip proof**~~ **done** (§4.2) | It was the finding that could change the plan. It did not: the rules ported cleanly and 105/105 documents agree |
| 2 | **statemachined** | §5.1 |
| 3 | **triald** | Commands statemachined; wants it stable first |
| 4 | **Retire the Python daemons** | Only after both run on rigs through a full session |

### 5.1 statemachined first, with a caveat

**For:** mousewheeld gives the most leverage exactly where the risk is — a board
over a serial link (§4.1). triald has no hardware, so it borrows almost nothing
from mousewheeld. And triald *commands* statemachined, so a stable statemachined
underneath it is worth having first.

**Against:** triald is smaller (7,785 vs 10,635 hand-written) and has no
hardware, so it would prove the process at lower risk.

**Recommendation: statemachined.** Doing the easy one first proves a process
that does not cover the hard part. The device layer is the thing that could
change the estimate, and mousewheeld is the help available for it.

### 5.2 A strangler is not available at runtime

With a fixed proto, rewriting rpc-by-rpc looks possible. **It is not**, for one
physical reason: statemachined owns a serial port, and two processes cannot own
one board. There is no split where both daemons run.

So each daemon is a cutover, and the safety comes from elsewhere: the e2e suite,
the document proof, and a rig running the new daemon for a full session beside
the old one's recordings.

---

## 6. What does not change

Worth stating, because it is most of the surface and it is why this is tractable:

- **`proto/`** — the interface. If it changes during the rewrite, something has
  gone wrong.
- **`client/python/`** — experimenters' scripts import this. It speaks gRPC and
  cannot tell what implements the other end. **It must not be touched**, and
  that it still passes is a strong signal.
- **`client/web/`** — panels and browser client, unchanged.
- **The firmware** — C, on the board.
- **The file formats** — see §4.2. The *parser* is rewritten; the format is not.
- **`contracts/`** — `braemons/v1/` stays canonical and vendored. A Rust daemon
  generates from the same `.proto` the Python one did.
- **The package archive** — `packages/` serves debs; a Rust binary is still a
  deb. `packaging/` changes, apt does not.

---

## 7. What gets deleted

| | lines |
|---|---|
| `statemachined/daemon/src/.../api/web_edge.py` | ~270 |
| `triald/daemon/src/triald/api/web_edge.py` | ~270 |
| `test_the_browser_edge.py` × 2 | ~400 |
| `grpc_port_for` and the second listener in both CLIs | — |
| uvicorn, grpcio, pydantic as runtime dependencies | — |

Roughly a thousand lines that exist only to compensate for the language.

---

## 8. Open questions

1. **Is there appetite for one rig running a Rust daemon for a real session
   before the Python one is retired?** Without that, §5.2 has no safety net
   beyond the test suites. This is the question that decides whether the plan is
   safe, and it is about lab time, not code.
2. ~~**Who maintains it?**~~ **Settled: Rust in the daemon is fine.** The
   "a physiologist can patch Python on a rig" argument does not survive contact
   with the rig: the board already runs C++ firmware that cannot easily be
   patched, so the parts of this system that are hard to change are hard to
   change already. A Rust daemon does not move that line.
3. **Does `device_clock_correlation.py` have a ported-arithmetic check** against
   recordings made by the Python daemon, or does it need one written first?
   (§4.1 assumes one is written.)
4. **Do the rigs have graph and config files not represented in the test
   corpus?** §4.2's proof is only as good as the corpus it runs on.
