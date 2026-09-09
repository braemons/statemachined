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
// preemptible desktop kernel, and docs/operations/hardware.md's numbers come from a board.
// Durations are honest to a millisecond or so, which is what an integration
// test needs and nothing more.
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>

#include "hal.h"
#include "io/input_conditioner.h"
#include "io/reply_queue.h"
#include "io/settings_store.h"
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

/// The store, over the HAL's file-backed one -- the same class main.cpp has,
/// for the same reason: the session touches no port, and this is a port.
///
/// It means the daemon's integration tests exercise saving and restoring
/// against a device that really does forget when the file is removed, rather
/// than against a mock that agrees with them.
class FileSettingsPort : public SettingsPort {
 public:
  bool has_storage() const override { return hal::storage_capacity() > 0; }

  bool save(const StoredSettings& s, uint32_t write_count) override {
    if (!hal::storage_write_begin()) return false;
    Sink sink;
    if (!save_settings(s, write_count, sink)) return false;
    return hal::storage_write_commit();
  }

  SettingsError load(StoredSettings& s) override {
    Source source;
    return load_settings(s, source);
  }

  bool holds(const StoredSettings& s, uint32_t write_count) override {
    Source source;
    return settings_already_stored(s, write_count, source);
  }

 private:
  class Sink : public SettingsWriter {
   public:
    bool write(const void* src, size_t n) override { return hal::storage_write(src, n); }
  };
  class Source : public SettingsReader {
   public:
    bool read(void* dst, size_t n) override {
      if (!hal::storage_read(at_, dst, n)) return false;
      at_ += n;
      return true;
    }

   private:
    size_t at_ = 0;
  };
};

}  // namespace

/// The software loopback harness, if the environment asked for one.
///
/// `STATEMACHINED_LOOPBACK=8` wires output line n back to input line
/// (n + 4) mod 8 -- the eight jumper wires of docs/operations/hardware.md, in software, so
/// that a suite driving transitions from predicates is the same suite with a
/// board and without one. `<width>:<shift>` sets both; unset is off.
///
/// An environment variable rather than an argument for the same reason
/// STATEMACHINED_STORE is one: this binary's whole command line is "no
/// arguments", and every launcher it has -- the socket bridge, `make
/// bench-device`, `statemachined device` -- already passes an environment.
void configure_the_loopback_from_the_environment() {
  const char* setting = std::getenv("STATEMACHINED_LOOPBACK");
  if (setting == nullptr || *setting == '\0') return;

  char* after = nullptr;
  const long width = std::strtol(setting, &after, 10);
  if (width <= 0 || width > 32) return;
  long shift = width / 2;  // out n -> in (n + width/2), the harness's own rule
  if (after != nullptr && *after == ':') shift = std::strtol(after + 1, nullptr, 10);

  hal::set_native_loopback(static_cast<uint8_t>(width), static_cast<uint8_t>(shift % width));
}

int main() {
  // Before init(), which primes the conditioner off the first read: a harness
  // configured after that would have its first scan see an input word that the
  // conditioner had already been told was the resting state.
  configure_the_loopback_from_the_environment();
  hal::init();

  QueueingReplySink reply_sink;
  static HostLinkSession session(reply_sink, native_device_identity());
  static FileSettingsPort settings;
  session.set_settings_port(&settings);
  // What this device remembers about itself, before anything is driven -- the
  // same order, and for the same reasons, as firmware/src/main.cpp.
  session.restore_settings(hal::micros_now());

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
