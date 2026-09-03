// SPDX-License-Identifier: GPL-3.0-or-later
// The Uno R4 Minima. Renesas RA4M1 at 48 MHz, 32 KB SRAM, native USB CDC.
//
// The reference target, and the one that sets the design: everything here is
// written against a 100 us scan period, which is what 10 kHz means on a 48 MHz
// core.
//
// This file is per board and nothing else is. Debounce, polarity, pulse widths
// and toggle direction all live in firmware/core, where the host tests reach
// them; what is left here is port registers, a timer and a serial port.
//
// Pinout: dev/HARDWARE.md.
#if defined(ARDUINO_ARCH_RENESAS) || defined(ARDUINO_UNOR4_MINIMA)

#include <Arduino.h>
#include <FspTimer.h>

#include "hal.h"

// Which port the host is on.
//
// A rig uses native USB CDC: one cable, no adapter, and the port appears when
// the bridge opens it. Under emulation it is SCI2 on D0/D1 instead, because
// tinyusb against an emulated USBFS is by far the most fragile thing in the
// picture and the point of the emulator is to test *our* code. It is also the
// right build for a rig that wants a hardware serial bridge rather than a USB
// one, so this is not a test-only switch.
//
// Only the three link functions differ. Pins, timer and clock are the same
// board either way.
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
// Eight in, eight out. The R4 Minima has more usable pins than that, but D0/D1
// are the UART and D13 carries the on-board LED, and a line map that quietly
// includes either is a line map that surprises somebody at 2 a.m.
//
// Order is the statemachined line number: kInputPins[0] is input line 0. Changing this
// table changes what every existing graph means, so it is a wire contract in
// the same sense the protocol is -- see dev/HARDWARE.md.
constexpr uint8_t kInputPins[] = {2, 3, 4, 5, 6, 7, 8, 9};
constexpr uint8_t kOutputPins[] = {10, 11, 12, A0, A1, A2, A3, A4};

constexpr uint8_t kInputCount = sizeof(kInputPins);
constexpr uint8_t kOutputCount = sizeof(kOutputPins);

static_assert(kInputCount <= 16, "an input word is 32 lines wide");
static_assert(kOutputCount <= 16, "an output word is 32 lines wide");

// ------------------------------------------------------- the port registers ---
//
// The RA4M1's GPIO ports are uniformly spaced, and every one has the same three
// registers. Deriving the address arithmetically means one code path for all of
// them instead of a switch over ten CMSIS pointers.
constexpr uintptr_t kPortBase = R_PORT0_BASE;
constexpr uintptr_t kPortStride = R_PORT1_BASE - R_PORT0_BASE;

inline volatile R_PORT0_Type* port(uint8_t n) {
  return reinterpret_cast<volatile R_PORT0_Type*>(kPortBase + kPortStride * n);
}

/// Which port and which bit an Arduino pin number lands on. Taken from the
/// core's own table rather than written out here: a hand-copied pin map is a
/// silent wrong-valve bug, and the core already knows the answer.
struct PortBit {
  uint8_t port;
  uint16_t mask;
};

PortBit port_bit_of(uint8_t arduino_pin) {
  const bsp_io_port_pin_t bsp = digitalPinToBspPin(arduino_pin);
  PortBit pb;
  pb.port = static_cast<uint8_t>(bsp >> 8);
  pb.mask = static_cast<uint16_t>(1u << (bsp & 0xFFu));
  return pb;
}

// ------------------------------------------------------------ the line map ---

/// Ports actually used, so a scan reads three registers rather than ten.
struct PortGroup {
  uint8_t port = 0;
  uint16_t mask = 0;  ///< every pin of ours on this port, for one masked write
};

PortBit in_[kInputCount];
PortBit out_[kOutputCount];

PortGroup in_ports_[kInputCount];
uint8_t in_port_count_ = 0;
/// For each input line, which slot of in_ports_ its port was cached into.
uint8_t in_slot_[kInputCount];

PortGroup out_ports_[kOutputCount];
uint8_t out_port_count_ = 0;
/// For each output port slot, the statemachined lines that live on it.
LineBitmask out_lines_[kOutputCount];

uint8_t intern_port(PortGroup* groups, uint8_t* count, uint8_t p) {
  for (uint8_t i = 0; i < *count; ++i)
    if (groups[i].port == p) return i;
  groups[*count].port = p;
  groups[*count].mask = 0;
  return (*count)++;
}

// ------------------------------------------------------------- the clock ---

FspTimer scan_timer_;
void (*scan_callback_)() = nullptr;

void on_scan_tick(timer_callback_args_t*) {
  if (scan_callback_ != nullptr) scan_callback_();
}

}  // namespace

void init() {
  // Outputs first, and low, before anything else is enabled. A board that
  // powers up with a valve line floating has already failed, whatever the
  // firmware does next. There is no graph yet, so "safe" is "off"; main.cpp
  // applies the graph's real safe levels the moment one is committed.
  for (uint8_t i = 0; i < kOutputCount; ++i) {
    pinMode(kOutputPins[i], OUTPUT);
    digitalWrite(kOutputPins[i], LOW);
    out_[i] = port_bit_of(kOutputPins[i]);
    const uint8_t slot = intern_port(out_ports_, &out_port_count_, out_[i].port);
    out_ports_[slot].mask |= out_[i].mask;
    out_lines_[slot] |= (1u << i);
  }

  for (uint8_t i = 0; i < kInputCount; ++i) {
    // INPUT, not INPUT_PULLUP: a rig's TTL sources drive both ways, and a pull-up
    // on a line an opto-isolator is sinking is a line that never reads low.
    // Anything needing a pull-up gets a resistor, which is visible on the bench.
    pinMode(kInputPins[i], INPUT);
    in_[i] = port_bit_of(kInputPins[i]);
    in_slot_[i] = intern_port(in_ports_, &in_port_count_, in_[i].port);
    in_ports_[in_slot_[i]].mask |= in_[i].mask;
  }

  // Real on a UART, ignored on native USB CDC, which runs at bus speed.
  STATEMACHINED_LINK.begin(921600);
}

LineBitmask read_inputs() {
  // One register read per port, not one per line. The Renesas core's
  // digitalRead() costs 1-2 us, so eight of them would be a fifth of the scan
  // budget before anything had been decided.
  uint16_t live[kInputCount];
  for (uint8_t p = 0; p < in_port_count_; ++p)
    live[p] = static_cast<uint16_t>(port(in_ports_[p].port)->PCNTR2 & 0xFFFFu);

  LineBitmask word = 0;
  for (uint8_t i = 0; i < kInputCount; ++i)
    if (live[in_slot_[i]] & in_[i].mask) word |= (1u << i);
  return word;
}

void write_outputs(LineBitmask set_high, LineBitmask set_low) {
  // PCNTR3 is set-and-reset in one write-only 32-bit register: POSR low, PORR
  // high. No read-modify-write, so this is safe against an ISR touching another
  // pin on the same port, and it costs one store per port however many lines
  // moved.
  //
  // A line in both masks is a bug upstream. Drive it high: a valve left open by
  // a caller's mistake is worse than one that opened when it should not have.
  const LineBitmask low = set_low & ~set_high;

  for (uint8_t p = 0; p < out_port_count_; ++p) {
    uint32_t sr = 0;
    for (LineBitmask rest = set_high & out_lines_[p]; rest != 0;) {
      const uint8_t line = static_cast<uint8_t>(__builtin_ctz(rest));
      rest &= ~(1u << line);
      sr |= out_[line].mask;
    }
    for (LineBitmask rest = low & out_lines_[p]; rest != 0;) {
      const uint8_t line = static_cast<uint8_t>(__builtin_ctz(rest));
      rest &= ~(1u << line);
      sr |= static_cast<uint32_t>(out_[line].mask) << 16;
    }
    if (sr != 0) port(out_ports_[p].port)->PCNTR3 = sr;
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
  // availableForWrite() is what makes this non-blocking. The core's write()
  // spins until the endpoint has accepted every byte it was given -- for USB
  // CDC that means waiting on the host's next poll -- so it is only ever handed
  // an amount the fifo has already said it has room for, and it returns without
  // waiting for anything.
  const int space = STATEMACHINED_LINK.availableForWrite();
  if (space <= 0) return 0;
  const size_t take = (static_cast<size_t>(space) < n) ? static_cast<size_t>(space) : n;
  return STATEMACHINED_LINK.write(src, take);
}

bool link_up() {
#if defined(STATEMACHINED_LINK_UART)
  // A UART has no DTR and no carrier: there is nothing to ask. Link loss on
  // this build is detectable only by the heartbeat lapsing, which is the
  // bridge's job, so saying "up" here is the honest answer rather than an
  // optimistic one.
  return true;
#else
  // USB CDC: false the moment the bridge closes the port. This is what a rig
  // should fail-safe on -- it is immediate, where a heartbeat timeout is not.
  return static_cast<bool>(STATEMACHINED_LINK);
#endif
}

// ---------------------------------------------------------- the scan timer ---

/// Not in hal.h: who owns the tick is a property of the board, and only
/// main.cpp calls it. Declared here and used there.
bool start_scan_timer(uint32_t hz, void (*callback)()) {
  scan_callback_ = callback;
  uint8_t type = 0;
  const int8_t ch = FspTimer::get_available_timer(type);
  if (ch < 0) return false;
  if (!scan_timer_.begin(TIMER_MODE_PERIODIC, type, static_cast<uint8_t>(ch),
                         static_cast<float>(hz), 50.0f, on_scan_tick, nullptr))
    return false;
  if (!scan_timer_.setup_overflow_irq()) return false;
  if (!scan_timer_.open()) return false;
  return scan_timer_.start();
}

}  // namespace hal
}  // namespace statemachined

#endif  // ARDUINO_ARCH_RENESAS
