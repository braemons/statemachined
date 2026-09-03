// SPDX-License-Identifier: GPL-3.0-or-later
// The whole hardware surface, and it is deliberately this small.
//
// Free functions rather than a virtual interface: read_inputs() and
// write_outputs() are called from a 10 kHz timer ISR, where an indirect call
// through a vtable buys nothing -- there is exactly one implementation linked
// into any given binary, chosen by the build, and the compiler can inline it.
// The seam that matters for testing is above this, at StateMachine and
// HostLinkSession, both of which are driven entirely by their arguments.
//
// Everything with a decision in it lives above this line. Debounce, polarity
// and enable are InputConditioner's; pulse widths and toggle direction are
// StateMachine's. What is left here is genuinely per board and cannot be
// anything else.
//
// Implementations: firmware/hal/native.cpp (host, for the simulator),
// firmware/hal/renesas_ra4m1.cpp (Uno R4 Minima). See dev/HARDWARE.md for which
// physical pin is which line.
#pragma once
#include <cstddef>
#include <cstdint>

#include "config.h"

namespace fsmd {
namespace hal {

/// Pin directions, the serial link, and the scan timer. Called once, before
/// anything else here.
///
/// It must leave every output at its reset-safe level before it enables
/// anything: a board that powers up with a valve line floating high has already
/// failed, whatever the firmware does next.
void init();

/// Every input line in one read, as the pins actually are -- not debounced, not
/// polarity-corrected, not masked. InputConditioner does all three.
///
/// One call for the whole word, rather than one per line, because this is the
/// hot path: on the RA4M1 the Renesas core's digitalRead() costs 1-2 us, so 32
/// of them would not fit in a 100 us scan even if nothing else happened. A HAL
/// reads the port registers directly.
LineBitmask read_inputs();

/// Drive lines. Both masks, because a scan that touches nothing must be
/// distinguishable from one that drives everything low, and because writing a
/// whole level word would fight anything else that owns a pin on the same port.
///
/// A line in neither mask is left exactly as it is. A line in both is a bug
/// upstream; drive it high, so that a valve is never left open by a mistake in
/// the caller.
void write_outputs(LineBitmask set_high, LineBitmask set_low);

/// The device clock, monotonic, free-running, wrapping every ~71 minutes. Every
/// duration in the system is measured as a difference, so the wrap is not an
/// event anything has to handle.
Microseconds micros_now();

// --------------------------------------------------------------- the link ---

/// Bytes from the host, however many are there. Never blocks: returns 0 when
/// there is nothing, which is the common case on a scan.
size_t link_read(char* dst, size_t max);

/// Bytes to the host. May block if the host is not draining the port, which is
/// why it is never called from the scan ISR -- see firmware/src/main.cpp.
void link_write(const char* src, size_t n);

/// Whether the host is there at all. On native USB CDC this is DTR, which goes
/// false when the bridge closes the port -- the signal a rig needs to fail-safe
/// on rather than waiting for a heartbeat to time out.
bool link_up();

}  // namespace hal
}  // namespace fsmd
