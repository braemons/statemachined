// Board entry point. Deliberately thin: everything with a decision in it is in
// core/, which knows nothing about Arduino and is tested on the host.
//
// What is decided *here*, and nowhere else, is what runs in the interrupt.
//
// ---------------------------------------------------------------------------
// The timer is a tick source, not the scan.
// ---------------------------------------------------------------------------
//
// The obvious design -- and Bpod's -- runs the state machine inside the timer
// ISR. We do not, and the reason is that this firmware parses JSON in the
// foreground. An ISR that advances the trial while loop() is halfway through
// HostLinkSession::receive() shares the runner, the graph and the reply buffer
// with it, on a single core with no lock worth the name at 100 us. The fixes
// are a critical section around every command (which stalls the scan for as
// long as a parse takes, which is the thing we were protecting) or a lock-free
// handoff of every command the ISR must apply (which is real, and is a rewrite
// of the session).
//
// So the ISR increments a counter and returns, and loop() does the scan. That
// makes the firmware correct by construction on day one and keeps the engine
// byte-identical to what the host tests exercise, which is the entire argument
// for the portable core.
//
// What it costs is jitter, not rate: if a link burst takes 300 us, three
// periods pass and one scan happens. That is a real cost and it is *counted*
// rather than absorbed -- ScanHealth::overruns and worst_gap go out in every
// state_report, so a rig that is stuttering says so instead of quietly
// measuring a response window on a clock that skipped. A design that fails
// loudly is worth more here than one that is faster and silent.
//
// If measurement on the board says the link stalls the scan too often, the
// known fix is to move the scan into the ISR behind a command handoff. That is
// a change to this file. Nothing in core/ moves.
#include <Arduino.h>

#include "hal.h"
#include "io/input_conditioner.h"
#include "protocol/host_link_session.h"

using namespace fsmd;

namespace fsmd {
namespace hal {
/// Declared in the board HAL rather than in hal.h: who owns the tick is a
/// property of the board, and this is the only caller.
bool start_scan_timer(uint32_t hz, void (*callback)());
}  // namespace hal
}  // namespace fsmd

namespace {

constexpr uint32_t kScanHz = 10000;

/// Lines this board actually has. The graph pools are sized for 32 so that one
/// binary's data structures do not change shape per board; what the board can
/// physically drive is reported in hello_ack, and the bridge checks a graph
/// against it before uploading a byte.
constexpr uint8_t kBoardInputLines = 8;
constexpr uint8_t kBoardOutputLines = 8;

class SerialReplySink : public ReplySink {
 public:
  void send_line(const char* bytes, size_t n) override { hal::link_write(bytes, n); }
};

SerialReplySink g_sink;
InputConditioner g_inputs;
ScanHealth g_health;

/// Ticks since boot, written by the ISR and read by loop(). A single aligned
/// 32-bit load on this core, so it needs no critical section -- and it is only
/// ever compared against our own count, never used as a clock.
volatile uint32_t g_ticks = 0;
uint32_t g_consumed = 0;

void on_tick() { ++g_ticks; }

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

void apply(const OutputUpdate& ops) {
  if ((ops.set_high | ops.set_low) != 0) hal::write_outputs(ops.set_high, ops.set_low);
}

void scan() {
  const Microseconds now = hal::micros_now();
  const LineBitmask word = g_inputs.apply(hal::read_inputs(), now);
  apply(g_session->advance_trial(word, now));
}

void service_link() {
  const bool up = hal::link_up();
  if (!up) {
    if (g_link_was_up) {
      // The bridge went away. Cancel anything in flight through the ordinary
      // exit path, then drive every line to its configured safe level -- there
      // is nobody to report to, which is precisely why the outputs cannot be
      // left for the host to sort out.
      const Microseconds now = hal::micros_now();
      apply(g_session->link_lost(now));
      apply(g_session->fail_safe());
      g_link_was_up = false;
    }
    return;
  }
  g_link_was_up = true;

  char buf[64];
  const size_t n = hal::link_read(buf, sizeof(buf));
  if (n > 0) g_session->receive(buf, n, hal::micros_now());
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
    apply(g_session->fail_safe());
    for (;;)
      ;
  }
}

void loop() {
  const uint32_t ticks = g_ticks;
  if (ticks != g_consumed) {
    const uint32_t elapsed = ticks - g_consumed;
    g_consumed = ticks;
    if (elapsed > 1) {
      // Periods that went by with no scan in them. Counted, never absorbed: a
      // board that quietly misses scans looks exactly like a board that is fine.
      const uint32_t missed = elapsed - 1;
      g_health.overruns += missed;
      if (missed > g_health.worst_gap) g_health.worst_gap = missed;
      g_session->report_scan_health(g_health);
    }
    scan();
  }
  service_link();
}
