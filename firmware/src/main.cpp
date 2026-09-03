// SPDX-License-Identifier: GPL-3.0-or-later
// Board entry point. Deliberately thin: everything with a decision in it is in
// core/, which knows nothing about Arduino and is tested on the host.
//
// What is decided *here*, and nowhere else, is what runs in the interrupt.
//
// ---------------------------------------------------------------------------
// The scan runs in the timer ISR, and the foreground stands aside for it.
// ---------------------------------------------------------------------------
//
// It did not always. The first design had the ISR increment a counter and
// loop() do the scan, which is correct by construction -- nothing is shared,
// because only one context ever touches the engine -- and it cost jitter rather
// than rate. The bet was that the foreground would be quick enough between
// scans. On hardware it was not, and the numbers say why:
//
//   per command, at 10 kHz, while a bridge was talking
//     draining the link   1240 us      <- vendor USB stack
//     reading the link     754 us      <- vendor USB stack
//     tud_task()           533 us      <- vendor USB stack, via link_up()
//     HostLinkSession      372 us      <- ours
//     the scan itself        7 us
//
// About 2.5 ms of every command goes into TinyUSB, and the scan was queued
// behind it: ~10 missed periods per command, climbing for as long as the host
// kept talking. The reply path was rewritten to queue rather than block first
// (see core/io/reply_queue.h) and it changed nothing measurable, which is what
// finally located the problem: it is not that the foreground *blocks*, it is
// that the foreground has milliseconds of USB work to do at all. No amount of
// making our own code polite fixes a scan that has to wait its turn behind a
// vendor stack.
//
// So the scan moved into the ISR, where it preempts all of that and keeps its
// period whatever the link is doing. The concurrency this creates is real but
// it is small, and the same measurement is what bounds it: of the ~3 ms a
// command costs, only the 372 us inside HostLinkSession touches anything the
// scan touches. So the foreground raises a flag around exactly that window and
// the ISR defers while it is up, scanning on the next tick instead. Everything
// expensive -- the USB milliseconds -- runs with the flag down and is preempted
// normally.
//
// Measured again afterwards, on the same board and the same traffic:
//
//                        before        after
//     overruns/command     9.9          3.0     (ping)
//                         16.0          9.1     (state_report)
//     worst_gap            103            9
//
// And the residue is no longer a mystery: 3.0 periods is 300 us, against the
// 374 us the hold measures for a ping, and 9.1 against 973 us for a state. The
// scan now loses *exactly* the time the foreground spends inside the session
// and not one period more. Shrinking it further means splitting `receive()` so
// that framing and parsing happen outside the hold and only dispatch is inside
// -- a change to core/, worth doing when a rig's traffic makes it worth doing,
// and not before.
//
// What is left is counted, not absorbed: a scan deferred by that window is an
// overrun like any other, and ScanHealth::overruns and worst_gap go out in
// every state_report. A rig that is stuttering says so rather than quietly
// measuring a response window on a clock that skipped.
//
// The engine itself is untouched by any of this. Nothing in core/ knows which
// context calls it, the host tests still exercise byte-identical code, and the
// whole handoff is the flag below and the two places that raise it.
#include <Arduino.h>

#include "demo/demo_graph.h"
#include "hal.h"
#include "io/input_conditioner.h"
#include "io/reply_queue.h"
#include "protocol/host_link_session.h"
#include "trial/trial_runner.h"

using namespace statemachined;

namespace statemachined {
namespace hal {
/// Declared in the board HAL rather than in hal.h: who owns the tick is a
/// property of the board, and this is the only caller.
bool start_scan_timer(uint32_t hz, void (*callback)());
}  // namespace hal
}  // namespace statemachined

namespace {

constexpr uint32_t kScanHz = 10000;

/// Lines this board actually has. The graph pools are sized for 32 so that one
/// binary's data structures do not change shape per board; what the board can
/// physically drive is reported in hello_ack, and the bridge checks a graph
/// against it before uploading a byte.
constexpr uint8_t kBoardInputLines = 8;
constexpr uint8_t kBoardOutputLines = 8;

InputConditioner g_inputs;
/// Health the board measures about itself and hands to the session for
/// state_report. Declared before the reply sink, which counts its stalls here.
ScanHealth g_health;

ReplyQueue g_tx;

/// Hand the link as much as it will take, and not one byte more.
///
/// Costs the scan only what the endpoint accepts immediately, which is the
/// whole point: what is left waits here rather than in a write() that spins.
void drain_tx() {
  const char* p = nullptr;
  size_t n = 0;
  while ((n = g_tx.peek(&p)) > 0) {
    const size_t wrote = hal::link_write_some(p, n);
    g_tx.consume(wrote);
    if (wrote < n) return;  // the link is full for now; the rest goes next pass
  }
}

class SerialReplySink : public ReplySink {
 public:
  void send_line(const char* bytes, size_t n) override {
    if (g_tx.push(bytes, n)) return;

    // The queue is full and this line has nowhere to go. That means a burst
    // bigger than the queue -- in practice the result chunks that end a trial,
    // which is when the trial is already over and jitter costs nothing. Waiting
    // here is still the only remaining way the link can stall the scan, so it
    // is counted rather than absorbed, exactly like an overrun.
    ++g_health.tx_stalls;
    while (!g_tx.push(bytes, n)) {
      if (!hal::link_up()) {
        // Nobody is going to read any of this. Dropping it is what link loss
        // means; spinning here would hang the board on an absent host.
        g_tx.clear();
        return;
      }
      drain_tx();
    }
  }
};

SerialReplySink g_sink;

/// Ticks since boot, written by the ISR and read by loop(). A single aligned
/// 32-bit load on this core, so it needs no critical section -- and it is only
/// ever compared against our own count, never used as a clock.
volatile uint32_t g_ticks = 0;
volatile uint32_t g_consumed = 0;

/// Raised while the foreground is inside the engine or the session, which is
/// the only window in which the ISR must not scan.
///
/// It is deliberately not "while the foreground is busy": the expensive part of
/// a command is the USB stack, and that shares nothing with a scan, so it runs
/// with this down and gets preempted like any other work. Measured at ~372 us
/// per command up, against ~2.5 ms down.
volatile bool g_engine_held = false;

/// The fixed cost of a scan, measured rather than declared, so the host learns
/// the timing resolution it is actually getting.
///
/// This is the floor: reading the pins and conditioning them, with no graph
/// committed. Evaluating a state's transitions is on top of it and depends on
/// the graph, which is why the live overrun count in state_report matters more
/// than this number does.
uint32_t measure_scan_floor_hz() {
  constexpr uint32_t kReps = 2000;
  const Microseconds t0 = hal::micros_now();
  for (uint32_t i = 0; i < kReps; ++i) {
    const LineBitmask w = g_inputs.apply(hal::read_inputs(), hal::micros_now());
    // Keep the optimiser from deleting the thing being measured.
    asm volatile("" ::"r"(w) : "memory");
  }
  const Microseconds dt = hal::micros_now() - t0;
  if (dt == 0) return 0;
  return static_cast<uint32_t>((static_cast<uint64_t>(kReps) * 1000000u) / dt);
}

DeviceIdentity make_identity() {
  DeviceIdentity id;
  id.board = "uno_r4_minima";
  id.firmware_version = "0.1.0";
  id.input_line_count = kBoardInputLines;
  id.output_line_count = kBoardOutputLines;
  id.measured_scan_hz = g_health.hz;
  return id;
}

/// Constructed on first use, which is after hal::init() and after the scan
/// floor has been measured -- an identity built at static-init time would
/// report a rate nothing had measured yet.
HostLinkSession& session() {
  static HostLinkSession s(g_sink, make_identity());
  return s;
}

HostLinkSession* g_session = nullptr;
bool g_link_was_up = false;

// ---------------------------------------------------------------------------
// Demo mode
// ---------------------------------------------------------------------------
//
// Until a host says `hello`, this board holds no graph and nothing arms a
// trial, so a board on a bench sits there doing nothing whatsoever. That is
// correct for a rig -- a device that runs a paradigm nobody uploaded is a
// hazard -- and it makes the first bring-up needlessly blind: you cannot tell a
// working board from a dead one, or find out whether your LEDs are on the pins
// you think they are, without writing a host first.
//
// So before the first `hello` the board runs demo::build()'s graph on its own
// runner. It is the real engine on a real graph: the same TrialRunner, the same
// validate(), the same conditioned input word. What it is not is a fallback
// paradigm -- the first `hello` ends it permanently (until reset) and hands the
// pins back through fail_safe(), so a rig cannot silently run the demo while
// somebody believes it is running an experiment.
// Costs ~4.6 KB of SRAM -- its own StateGraph and its own TrialRunner -- on a
// board with 32 KB. That is worth it on a bench and worth nothing on a rig,
// where a host greets within a second of boot, so a deployed build can drop it
// with -DSTATEMACHINED_DEMO=0 and get the RAM back. The switch itself lives in
// firmware/core/config.h, with the capacities that depend on it.

#if STATEMACHINED_DEMO
StateGraph g_demo_graph;
bool g_demo_active = false;
uint32_t g_demo_trial_id = 0;
Microseconds g_demo_start_at_us = 0;
bool g_demo_waiting_to_start = false;
bool g_demo_led = false;

/// Fixed, so the bench replays: the demo draws no random timings today, but it
/// runs through the code path that would.
constexpr uint64_t kDemoSeed = 0xF5D0DE30F5D0DE30ULL;

/// How long the outcome lamp stays lit before the next trial arms. A terminal
/// state's entry actions run and nothing exits it, so the lamp is still on --
/// this is the pause that makes it readable rather than a flicker.
constexpr Microseconds kDemoRelightPauseUs = 1500u * 1000u;

/// Constructed on first use so it binds to a graph that has been built. Holds a
/// pointer to it, not a copy.
TrialRunner& demo_runner() {
  static TrialRunner r(g_demo_graph);
  return r;
}

/// Wall-clock comparison that survives the microsecond counter wrapping every
/// ~71 minutes. A bench board is left running for longer than that.
bool reached(Microseconds now, Microseconds deadline) {
  return static_cast<int32_t>(now - deadline) >= 0;
}

/// Defined below, next to the rest of demo mode; declared here because
/// service_link() is what notices a host has arrived.
void demo_end(Microseconds now);

#else  // !STATEMACHINED_DEMO

// Stubs, so the call sites read the same in both builds and the compiler drops
// the branches rather than the reader having to.
constexpr bool g_demo_active = false;
inline void demo_begin(Microseconds) {}
inline void demo_end(Microseconds) {}
inline void demo_scan(LineBitmask, Microseconds) {}

#endif  // STATEMACHINED_DEMO

void apply_wiring();

void apply(const OutputUpdate& ops) {
  if ((ops.set_high | ops.set_low) != 0) hal::write_outputs(ops.set_high, ops.set_low);
}

void scan() {
  const Microseconds now = hal::micros_now();
  const LineBitmask word = g_inputs.apply(hal::read_inputs(), now);
  apply(g_session->advance_trial(word, now));
}

#if STATEMACHINED_DEMO
/// Defined below with the rest of demo mode; the scan has to be able to choose
/// between it and the session's before either is in scope.
void demo_scan(LineBitmask word, Microseconds now);
#endif

/// One scan, wherever it is called from. Both callers hold the engine.
void scan_once() {
#if STATEMACHINED_DEMO
  if (g_demo_active) {
    const Microseconds now = hal::micros_now();
    demo_scan(g_inputs.apply(hal::read_inputs(), now), now);
    return;
  }
#endif
  scan();
}

/// Account for periods that went by with no scan in them, and remember the
/// worst run of them. Counted, never absorbed: a board that quietly misses
/// scans looks exactly like a board that is fine.
void note_missed(uint32_t missed) {
  if (missed == 0) return;
  g_health.overruns += missed;
  if (missed > g_health.worst_gap) g_health.worst_gap = missed;
}

/// The timer. Scans unless the foreground is inside the engine, in which case
/// it leaves the tick outstanding and loop() catches up the moment it is out.
void on_tick() {
  ++g_ticks;
  if (g_engine_held) return;
  const uint32_t ticks = g_ticks;
  note_missed(ticks - g_consumed - 1);
  g_consumed = ticks;
  scan_once();
}

/// Take the engine away from the ISR for the duration of a scope.
///
/// Interrupts are disabled only long enough to raise the flag, never for the
/// work itself. That is what makes the handoff airtight rather than hopeful: if
/// the ISR is mid-scan when this runs, noInterrupts() waits for it to finish --
/// an ISR cannot be preempted by the foreground -- and once the flag is up the
/// next tick defers instead of entering. So the two can never be inside the
/// engine at once, and the cost is a couple of microseconds rather than a
/// parse's worth of deaf timer.
class EngineHold {
 public:
  EngineHold() {
    noInterrupts();
    g_engine_held = true;
    interrupts();
  }
  ~EngineHold() {
    // Whatever the ISR could not do while we held it, do now, so a command does
    // not leave the trial a scan behind.
    const uint32_t ticks = g_ticks;
    if (ticks != g_consumed) {
      note_missed(ticks - g_consumed - 1);
      g_consumed = ticks;
      scan_once();
    }
    g_engine_held = false;
  }
  EngineHold(const EngineHold&) = delete;
  EngineHold& operator=(const EngineHold&) = delete;
};

void service_link() {
  const bool up = hal::link_up();
  if (!up) {
    if (g_link_was_up) {
      // The bridge went away. Cancel anything in flight through the ordinary
      // exit path, then drive every line to its configured safe level -- there
      // is nobody to report to, which is precisely why the outputs cannot be
      // left for the host to sort out.
      const Microseconds now = hal::micros_now();
      const EngineHold hold;
      apply(g_session->link_lost(now));
      apply(g_session->fail_safe());
      // Whatever was still queued was for a host that is gone. Keeping it would
      // mean answering, after the port reopens, a message_id from a session
      // that no longer exists. Inside the hold, because clear() is the one
      // queue operation that moves both ends.
      g_tx.clear();
      g_link_was_up = false;
    }
    return;
  }
  g_link_was_up = true;

  char buf[64];
  const size_t n = hal::link_read(buf, sizeof(buf));
  if (n > 0) {
    const Microseconds now = hal::micros_now();
    // The hold covers the parse, the demo handover and the graph's input
    // configuration -- everything that touches the session, the runner or the
    // conditioner, and nothing else. Reading those bytes off USB, above, cost
    // twice as long and needed no hold at all.
    const EngineHold hold;
    g_session->receive(buf, n, now);
    // The first `hello` takes the board out of demo mode for good. Checked
    // after receive() rather than on link_up(), because opening the port is not
    // the same as a host being there -- a serial monitor does the former.
    if (g_demo_active && g_session->state() != LinkState::Greeting) demo_end(now);
    apply_wiring();
  }
}

#if STATEMACHINED_DEMO

/// The heartbeat on the board's own LED, which is deliberately NOT one of the
/// eight output lines: D13 is excluded from the line map on purpose, so this
/// cannot collide with anything a graph drives. A board blinking here is a
/// board that booted, started its timer and is scanning -- visible with nothing
/// wired to it at all.
///
/// Written only when it changes. digitalWrite() costs a microsecond or two on
/// this core and this runs inside the scan.
void demo_heartbeat(Microseconds now) {
  const bool on = (now % 1000000u) < 60000u;
  if (on == g_demo_led) return;
  g_demo_led = on;
  digitalWrite(LED_BUILTIN, on ? HIGH : LOW);
}

void demo_begin(Microseconds now) {
  demo::build(g_demo_graph);
  // The demo's debounce has to actually reach the conditioner, which is the
  // same path a `wiring` command's takes below. It is the demo's own wiring and
  // not the session's: a bench board has whatever is clipped to it, not a rig.
  const DeviceWiring demo_wiring = demo::wiring();
  g_inputs.configure(demo_wiring.inputs);
  g_inputs.prime(hal::read_inputs());
  pinMode(LED_BUILTIN, OUTPUT);
  g_demo_active = true;
  g_demo_waiting_to_start = true;
  g_demo_start_at_us = now;
}

/// A host arrived. Stop, and give every line back.
void demo_end(Microseconds now) {
  if (!g_demo_active) return;
  g_demo_active = false;
  TrialRunner& r = demo_runner();
  // Through the ordinary exit path, like any other cancel: whatever the current
  // state raised comes down by the code that always lowers it.
  if (r.running()) {
    r.cancel(TrialCancelReason::Host, now);
    apply(r.advance(g_inputs.word(), now));
  }
  g_demo_led = false;
  digitalWrite(LED_BUILTIN, LOW);

  // The demo's input configuration must not outlive it. The conditioner is
  // shared with the session, so leaving the demo's 20 ms debounce installed
  // means a host is silently reading lines 0 and 1 through the demo's idea of
  // them -- a difference nothing on the host can see. Back to the session's
  // wiring, which is the compile-time default until a `wiring` command has
  // arrived, and re-primed because the accepted levels were reached under the
  // old configuration.
  g_inputs.configure(g_session->wiring().inputs);
  g_inputs.prime(hal::read_inputs());

  // The session owns the pins from here, and it has no graph yet, so this is
  // every output line low.
  apply(g_session->fail_safe());
}

void demo_scan(LineBitmask word, Microseconds now) {
  demo_heartbeat(now);
  TrialRunner& r = demo_runner();

  if (g_demo_waiting_to_start) {
    // Pulses and other debts are still owed while nothing is running.
    apply(r.service_outputs(now));
    if (!reached(now, g_demo_start_at_us)) return;
    g_demo_waiting_to_start = false;
    apply(r.start(++g_demo_trial_id, kDemoSeed, now, word));
    return;
  }

  apply(r.advance(word, now));
  if (!r.running()) {
    g_demo_waiting_to_start = true;
    g_demo_start_at_us = now + kDemoRelightPauseUs;
  }
}

/// Push a newly committed graph's input configuration into the conditioner.
///
/// A graph declares invert, enable and per-line debounce, and until this existed
/// none of it reached the pins: the conditioner was default-constructed at boot
/// and never told about any graph, so an opto-isolated active-low input read
/// back inverted and every declared debounce was silently ignored. The
/// conditioner is owned here rather than by the session, so this is where the
/// two are joined.
#endif  // STATEMACHINED_DEMO

/// Push a changed wiring into the conditioner. Keyed on the session's
/// revision counter rather than on the graph version, which is the whole
/// difference M4b made: conditioning is a property of the box, so changing a
/// debounce no longer means re-uploading a paradigm, and uploading a paradigm
/// no longer silently re-conditions the inputs.
void apply_wiring() {
  static uint16_t applied = 0;
  const uint16_t revision = g_session->wiring_revision();
  if (revision == applied) return;
  applied = revision;
  // The demo owns the conditioner while it runs, and puts the session's wiring
  // back when it ends. Installing it underneath would give a bench board a
  // debounce it is not expecting mid-chase.
  if (g_demo_active) return;
  g_inputs.configure(g_session->wiring().inputs);
  // No previous level for the new polarity to be measured against, so adopt
  // what is there rather than reporting every line as having just moved.
  g_inputs.prime(hal::read_inputs());
}

}  // namespace

void setup() {
  hal::init();
  g_health.hz = measure_scan_floor_hz();
  g_inputs.prime(hal::read_inputs());
  g_session = &session();
  g_session->report_scan_health(g_health);
  // Every line to its safe level before the first scan. With no graph yet that
  // is all low, and it is applied again the moment a graph is committed.
  apply(g_session->fail_safe());
  if (!hal::start_scan_timer(kScanHz, on_tick)) {
    // Without the timer nothing advances a trial, so the board would sit there
    // accepting graphs and arming trials that then never end. Halt with every
    // line at its safe level instead: a board that is obviously dead is a much
    // better failure than one that looks healthy and silently never scans.
    //
    // Nothing here can report why, because the fault is at the point where the
    // link has not yet been serviced. The bridge sees a device that never
    // answers `hello`, which is at least unambiguous.
    apply(g_session->fail_safe());
    for (;;) {
    }
  }

  // Nothing has greeted us yet, so run the demo until something does. Last in
  // setup() because it needs the timer: without a tick nothing would advance it
  // and a lit ready lamp would be the whole show.
  demo_begin(hal::micros_now());
}

void loop() {
  // The ISR keeps these; the session only ever reads them, when a state_report
  // is built. Handing them over here rather than from the ISR keeps the
  // interrupt to the scan and nothing else -- and a diagnostic counter read one
  // pass late is still the truth about a board that skipped.
  g_session->report_scan_health(g_health);

  // No scan here any more: the timer does it, on time, whatever this is doing.
  // What is left is the link -- milliseconds of vendor USB stack that share
  // nothing with the engine and are preempted rather than waited for.
  service_link();
  // Last, so a reply produced by the command just serviced starts moving in
  // this same pass instead of waiting for the next one.
  drain_tx();
}
