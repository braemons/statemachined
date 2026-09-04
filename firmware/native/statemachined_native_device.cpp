// SPDX-License-Identifier: GPL-3.0-or-later
// The firmware, on this machine, speaking the real protocol down a pipe.
//
// firmware/src/main.cpp is the board's entry point and cannot run here: it is
// Arduino, it wants a timer peripheral, and its whole subject is what runs in
// an interrupt. This is the same session, the same engine and the same wire,
// driven by an ordinary loop -- so that the daemon can be integration-tested
// against the firmware rather than against a mock of it.
//
// What that buys is the class of bug a mock cannot have. A fake device answers
// what the test author believed the protocol says; this one answers what
// firmware/core/protocol says, refuses what it refuses, and reassembles a
// result out of the same chunker. dev/PLAN.md's testing section asks for
// exactly this: "whole trials, cancel races, link loss", on the host, with no
// board attached.
//
// What it is NOT is a timing test. The scan here is a nanosleep in a loop on a
// preemptible desktop kernel, and dev/HARDWARE.md's numbers come from a board.
// Durations are honest to a millisecond or so, which is what an integration
// test needs and nothing more.
#include <unistd.h>

#include <cstdio>
#include <cstring>
#include <ctime>

#include "hal.h"
#include "io/input_conditioner.h"
#include "io/reply_queue.h"
#include "protocol/host_link_session.h"

using namespace statemachined;

namespace {

/// The same period the reference board scans at, so a graph's timings behave
/// the way they will on hardware even though the jitter will not.
constexpr uint32_t kScanHz = 10000;
constexpr long kScanPeriodNanoseconds = 1000000000L / kScanHz;

ReplyQueue reply_queue;
InputConditioner input_conditioner;

/// Hand the link as much as it will take. On a board this is the difference
/// between a reply and ten missed scans; here it is simply the same code path.
void drain_reply_queue() {
  const char* bytes = nullptr;
  size_t available = 0;
  while ((available = reply_queue.peek(&bytes)) > 0) {
    const size_t written = hal::link_write_some(bytes, available);
    reply_queue.consume(written);
    if (written < available) return;
  }
}

class QueueingReplySink : public ReplySink {
 public:
  void send_line(const char* bytes, size_t length) override {
    // A burst bigger than the queue is the result that ends a trial. On a board
    // that costs the scan its periods and is counted; here there is no timer to
    // be late for, so waiting for the pipe is simply what happens.
    while (!reply_queue.push(bytes, length)) drain_reply_queue();
  }
};

DeviceIdentity native_device_identity() {
  DeviceIdentity identity;
  identity.board = "native";
  identity.firmware_version = "0.0.0";
  identity.input_line_count = kMaxLines;
  identity.output_line_count = kMaxOutputLines;
  // Declared rather than measured, unlike a board's: there is no scan floor to
  // measure on a machine that is also running a browser.
  identity.measured_scan_hz = kScanHz;
  identity.input_pin_labels = hal::input_pin_labels();
  identity.output_pin_labels = hal::output_pin_labels();
  return identity;
}

void apply_output_update(const OutputUpdate& update) {
  if ((update.set_high | update.set_low) != 0)
    hal::write_outputs(update.set_high, update.set_low);
}

}  // namespace

int main() {
  hal::init();

  QueueingReplySink reply_sink;
  static HostLinkSession session(reply_sink, native_device_identity());

  ScanHealth scan_health;
  scan_health.hz = kScanHz;
  session.report_scan_health(scan_health);

  input_conditioner.configure(session.wiring().inputs);
  input_conditioner.prime(hal::read_inputs());
  // Every line to its safe level before the first scan, exactly as main.cpp
  // does it -- and, as there, the levels come from the wiring rather than from
  // a graph, so a device holding no graph still fails safe correctly.
  apply_output_update(session.fail_safe());

  uint16_t applied_wiring_revision = session.wiring_revision();

  for (;;) {
    const Microseconds now_us = hal::micros_now();

    char inbound[128];
    const size_t received = hal::link_read(inbound, sizeof(inbound));
    if (received > 0) {
      session.receive(inbound, received, now_us);
      if (session.wiring_revision() != applied_wiring_revision) {
        applied_wiring_revision = session.wiring_revision();
        input_conditioner.configure(session.wiring().inputs);
        input_conditioner.prime(hal::read_inputs());
      }
    }

    const LineBitmask word = input_conditioner.apply(hal::read_inputs(), now_us);
    apply_output_update(session.advance_trial(word, now_us));
    drain_reply_queue();

    timespec scan_period{0, kScanPeriodNanoseconds};
    nanosleep(&scan_period, nullptr);
  }
}
