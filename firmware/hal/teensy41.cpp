// SPDX-License-Identifier: GPL-3.0-or-later
// The Teensy 4.1. NXP i.MX RT1062 at 600 MHz, 1 MB of RAM, native USB CDC.
//
// The headroom target. Nothing in the design needs what it has, and nothing
// here is faster than it has to be: the scan is the same 10 kHz the reference
// board runs, so a graph behaves the same on either and the difference is only
// how large a graph fits.
//
// **3.3 V, and not 5 V tolerant.** The R4 Minima is a 5 V board and a rig
// wired for it drives its inputs at 5 V; the same wires on this board damage
// the pin. A rig moving from one to the other needs level shifting on every
// input, not just a new line map.
//
// Pinout: docs/operations/hardware.md.
#if defined(ARDUINO_TEENSY41)

#include <Arduino.h>
#include <IntervalTimer.h>
#include <LittleFS.h>

#include "hal.h"
#include "profile/profile.h"

#if defined(STATEMACHINED_LINK_UART)
#define STATEMACHINED_LINK Serial1
#else
#define STATEMACHINED_LINK Serial
#endif

namespace statemachined {
namespace hal {
namespace {

// ------------------------------------------------------------ the pinout ---
//
// Eight in, eight out, laid out like the reference board's so that a harness
// moves across with the fewest surprises: 0/1 are Serial1, 13 is the on-board
// LED, and neither is a line.
//
// Order is the statemachined line number, and changing it changes what every
// graph means -- see docs/operations/hardware.md.
constexpr uint8_t kInputPins[] = {2, 3, 4, 5, 6, 7, 8, 9};
constexpr uint8_t kOutputPins[] = {10, 11, 12, 14, 15, 16, 17, 18};

// The numbers printed on the board, which is how PJRC's card names every pin.
// Deliberately not "D2" or "A0": those are the reference board's labels, and a
// line map written against them must fail to resolve here rather than resolve
// cleanly onto different holes. 14-18 are also A0-A4; the number is the one
// that is on the silkscreen.
constexpr const char* kInputPinLabels[] = {"2", "3", "4", "5", "6", "7", "8", "9"};
constexpr const char* kOutputPinLabels[] = {"10", "11", "12", "14", "15", "16", "17", "18"};

static_assert(sizeof(kInputPinLabels) / sizeof(kInputPinLabels[0]) == sizeof(kInputPins),
              "every input line needs exactly one label");
static_assert(sizeof(kOutputPinLabels) / sizeof(kOutputPinLabels[0]) == sizeof(kOutputPins),
              "every output line needs exactly one label");

constexpr uint8_t kInputCount = sizeof(kInputPins);
constexpr uint8_t kOutputCount = sizeof(kOutputPins);

// ------------------------------------------------------- the port registers ---
//
// The startup code routes every pin to the fast GPIO6-9 ports, each of which
// has a data-input register and set and clear registers that touch only the
// bits written. The core's table says which register and which bit a pin is,
// so nothing here copies its pin map.

struct PortGroup {
  volatile uint32_t* psr = nullptr;    ///< read side: the pad status register
  volatile uint32_t* set = nullptr;    ///< DR_SET
  volatile uint32_t* clear = nullptr;  ///< DR_CLEAR
};

uint32_t in_mask_[kInputCount];
uint32_t out_mask_[kOutputCount];

PortGroup in_ports_[kInputCount];
uint8_t in_port_count_ = 0;
uint8_t in_slot_[kInputCount];

PortGroup out_ports_[kOutputCount];
uint8_t out_port_count_ = 0;
LineBitmask out_lines_[kOutputCount];

uint8_t intern_port(PortGroup* groups, uint8_t* count, const PortGroup& g) {
  for (uint8_t i = 0; i < *count; ++i)
    if (groups[i].psr == g.psr) return i;
  groups[*count] = g;
  return (*count)++;
}

PortGroup port_of(uint8_t pin) {
  PortGroup g;
  g.psr = portInputRegister(pin);
  g.set = portSetRegister(pin);
  g.clear = portClearRegister(pin);
  return g;
}

// ------------------------------------------------------------- the clock ---

IntervalTimer scan_timer_;

}  // namespace

void init() {
  // Outputs first, and low, before anything else is enabled. See hal.h.
  for (uint8_t i = 0; i < kOutputCount; ++i) {
    pinMode(kOutputPins[i], OUTPUT);
    digitalWriteFast(kOutputPins[i], LOW);
    out_mask_[i] = digitalPinToBitMask(kOutputPins[i]);
    const uint8_t slot = intern_port(out_ports_, &out_port_count_, port_of(kOutputPins[i]));
    out_lines_[slot] |= (1u << i);
  }

  for (uint8_t i = 0; i < kInputCount; ++i) {
    // INPUT, not INPUT_PULLUP, for the reason the reference board's HAL gives:
    // a pull-up on a line an opto-isolator is sinking never reads low.
    pinMode(kInputPins[i], INPUT);
    in_mask_[i] = digitalPinToBitMask(kInputPins[i]);
    in_slot_[i] = intern_port(in_ports_, &in_port_count_, port_of(kInputPins[i]));
  }

  // Real on a UART, ignored on native USB CDC, which runs at bus speed.
  STATEMACHINED_LINK.begin(921600);

#if defined(STATEMACHINED_PROFILE)
  // The startup code already has the M7's cycle counter running. At 600 MHz it
  // wraps every ~7 s, and every use is a difference far shorter.
  profile::set_clock([]() -> uint32_t { return ARM_DWT_CYCCNT; }, F_CPU_ACTUAL);
#endif
}

const char* board_name() { return "teensy41"; }
const char* const* input_pin_labels() { return kInputPinLabels; }
const char* const* output_pin_labels() { return kOutputPinLabels; }

LineBitmask read_inputs() {
  // One register read per port. Eight digitalReadFast() calls would be cheap
  // on this core too, but a scan that samples every line in one read is a scan
  // whose lines were all sampled at the same instant.
  uint32_t live[kInputCount];
  for (uint8_t p = 0; p < in_port_count_; ++p) live[p] = *in_ports_[p].psr;

  LineBitmask word = 0;
  for (uint8_t i = 0; i < kInputCount; ++i)
    if (live[in_slot_[i]] & in_mask_[i]) word |= (1u << i);
  return word;
}

void write_outputs(LineBitmask set_high, LineBitmask set_low) {
  // DR_SET and DR_CLEAR change only the bits written, so there is no
  // read-modify-write to race an ISR on the same port. Low first, then high:
  // a line in both masks is a bug upstream, and it ends up high (hal.h).
  for (uint8_t p = 0; p < out_port_count_; ++p) {
    uint32_t hi = 0;
    uint32_t lo = 0;
    for (LineBitmask rest = (set_high | set_low) & out_lines_[p]; rest != 0;) {
      const uint8_t line = static_cast<uint8_t>(__builtin_ctz(rest));
      rest &= ~(1u << line);
      if (set_high & (1u << line))
        hi |= out_mask_[line];
      else
        lo |= out_mask_[line];
    }
    if (lo != 0) *out_ports_[p].clear = lo;
    if (hi != 0) *out_ports_[p].set = hi;
  }
}

Microseconds micros_now() { return micros(); }

size_t link_read(char* dst, size_t max) {
  size_t n = 0;
  while (n < max && STATEMACHINED_LINK.available() > 0) {
    const int c = STATEMACHINED_LINK.read();
    if (c < 0) break;
    dst[n++] = static_cast<char>(c);
  }
  return n;
}

size_t link_write_some(const char* src, size_t n) {
  // Both the USB and the UART class answer availableForWrite() truthfully on
  // this core, so one path serves either: hand write() only what it already
  // has room for, and it returns without waiting on the host.
  const int space = STATEMACHINED_LINK.availableForWrite();
  if (space <= 0) return 0;
  const size_t take = (static_cast<size_t>(space) < n) ? static_cast<size_t>(space) : n;
  return STATEMACHINED_LINK.write(src, take);
}

bool link_up() {
#if defined(STATEMACHINED_LINK_UART)
  // No DTR on a UART; see the reference board's HAL.
  return true;
#else
  // USB CDC: configured, and DTR raised by a host that has opened the port.
  return static_cast<bool>(STATEMACHINED_LINK);
#endif
}

// -------------------------------------------------------------- the store ---
//
// One file on a small LittleFS in the program flash, above the image. The
// core's EEPROM emulation was the other candidate and is the wrong shape: 4 KB,
// smaller than the graph set this board's pools can hold, and written a byte at
// a time through its wear levelling.
//
// The file is rewritten whole, which on LittleFS is copy-on-write: an
// interrupted save leaves the previous file or none, never a mixture.
//
// **A save stops the scan.** The core runs every flash erase and program with
// interrupts off, because the image itself executes from the same flash. A
// save of the bench config's three graphs measured 13 missed scans (1.3 ms),
// which state_report counts. Save between sessions, as a rig does anyway
// (host_link_session.cpp).

namespace {

/// What LittleFS is given: eight of the 4.1's 64 KB erase blocks. Not one, which
/// is what this first asked for and which cannot even be formatted -- LittleFS
/// keeps its superblock in a pair of blocks, and a rewritten file needs a block
/// beside the one it replaces. Found on the board: the mount failed and the
/// board reported nowhere to keep settings. The record itself is held to 32 KB.
constexpr uint32_t kFilesystemBytes = 512u * 1024u;
constexpr size_t kStoreBytes = 32u * 1024u;
constexpr const char* kStoreFile = "/settings";

LittleFS_Program fs_;
bool fs_open_ = false;
bool fs_failed_ = false;

File writing_;
size_t write_at_ = 0;
bool write_failed_ = false;

/// Mounted on the first use rather than in init(), as on the reference board:
/// a board that never saves should not pay for it at boot. A mount that fails
/// once stays failed, and the board is a board without storage.
bool open_store() {
  if (fs_open_) return true;
  if (fs_failed_) return false;
  if (!fs_.begin(kFilesystemBytes)) {
    fs_failed_ = true;
    return false;
  }
  fs_open_ = true;
  return true;
}

}  // namespace

size_t storage_capacity() { return open_store() ? kStoreBytes : 0; }

bool storage_read(size_t offset, void* dst, size_t n) {
  if (!open_store()) return false;
  if (offset + n > kStoreBytes) return false;
  File f = fs_.open(kStoreFile, FILE_READ);
  if (!f) return false;
  const bool ok = f.seek(offset) && f.read(dst, n) == n;
  f.close();
  return ok;
}

bool storage_write_begin() {
  if (!open_store()) return false;
  if (writing_) writing_.close();
  write_at_ = 0;
  write_failed_ = false;
  // FILE_WRITE_BEGIN truncates: the old record is gone the moment this
  // succeeds, which is the erase hal.h asks for.
  writing_ = fs_.open(kStoreFile, FILE_WRITE_BEGIN);
  if (!writing_) {
    write_failed_ = true;
    return false;
  }
  return true;
}

bool storage_write(const void* src, size_t n) {
  if (write_failed_ || !writing_) return false;
  if (write_at_ + n > kStoreBytes || writing_.write(static_cast<const uint8_t*>(src), n) != n) {
    write_failed_ = true;
    return false;
  }
  write_at_ += n;
  return true;
}

bool storage_write_commit() {
  if (!writing_) return false;
  writing_.close();  // the flush, and on LittleFS the point the file exists
  return !write_failed_;
}

// ---------------------------------------------------------- the scan timer ---

/// Not in hal.h; see the reference board's HAL.
bool start_scan_timer(uint32_t hz, void (*callback)()) {
  // Above the USB interrupt, which the core leaves at the default 128: the
  // scan must preempt the vendor stack, never wait behind it.
  scan_timer_.priority(64);
  return scan_timer_.begin(callback, 1000000.0f / static_cast<float>(hz));
}

}  // namespace hal
}  // namespace statemachined

#endif  // ARDUINO_TEENSY41
