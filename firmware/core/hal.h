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
// firmware/hal/renesas_ra4m1.cpp (Uno R4 Minima). See docs/operations/hardware.md for which
// physical pin is which line.
#pragma once
#include <cstddef>
#include <cstdint>

#include "config.h"

namespace statemachined {
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

/// Bytes to the host, as many as the link will take *right now*. Returns how
/// many it took, which may be 0.
///
/// Never blocks, and that is the contract rather than a courtesy: the scan runs
/// in the foreground, so a write that waits for the host to poll its endpoint
/// costs scan periods. Measured on the reference board before this existed --
/// roughly 10 missed periods for a 65-byte reply and 16 for a 290-byte one, on
/// every command. Callers queue what is left (see core/io/reply_queue.h) and
/// offer it again on the next pass.
size_t link_write_some(const char* src, size_t n);

/// Whether the host is there at all. On native USB CDC this is DTR, which goes
/// false when the bridge closes the port -- the signal a rig needs to fail-safe
/// on rather than waiting for a heartbeat to time out.
bool link_up();

// -------------------------------------------------------------- the store ---
//
// Settings storage: data flash on a board, a file on the host. Written in one
// pass, whole -- the record has a CRC over all of it and is accepted only
// intact, so there is nothing to be gained by updating part of one.
//
// Buffered by the implementation rather than by the caller. Data flash programs
// in small fixed units and erases in larger ones, and a caller that had to know
// which would be a caller that knows the part number; the settings encoder just
// hands over bytes in order. See core/io/settings_store.h for why that matters:
// the largest thing stored is the graph set, and staging a copy of it would
// double the largest structure in the system.

/// How many bytes of settings storage this build has, or 0 for a board with
/// none -- which is a board whose settings do not survive a power cut, not a
/// board that fails. Everything above this treats storage as optional.
size_t storage_capacity();

/// Read `n` bytes from `offset`. False if the range is outside the store, which
/// a truncated record reads as.
bool storage_read(size_t offset, void* dst, size_t n);

/// Begin a whole-store write, discarding what is there. On a board this is the
/// erase, which is why an interrupted save reads back as a blank store rather
/// than as a mixture of two records.
bool storage_write_begin();

/// Append bytes to the write in progress. False once the store is full, and
/// once anything has failed: a failed write stays failed, so the caller does
/// not have to check every call to find out that the first one did not take.
bool storage_write(const void* src, size_t n);

/// Flush whatever is left, padding to whatever unit the part programs in. False
/// if any part of the write failed, in which case the store now holds no valid
/// record -- which is the honest outcome and the one the settings CRC turns
/// into "this board has forgotten", never into "this board believes something
/// wrong".
bool storage_write_commit();

// ---------------------------------------------------------------- the pins ---

/// What is written on the board beside each line, indexed by line number, or
/// nullptr where this build has no pins worth naming.
///
/// These come from the same table that `init()` calls pinMode() over, and that
/// is the whole point of them. Which pin a line is, and which direction it has,
/// are decided when this firmware is compiled; nothing on the wire changes
/// either. A host that wants to know therefore has to be *told* by the board,
/// and the alternative -- a table in the host keyed by the board name -- is a
/// hand-copied pin map, which is what the RA4M1 HAL refuses to keep of the
/// Arduino core's for exactly the reason it would be wrong here.
///
/// Answered to the host by the `pins` command. See docs/reference/protocol.md 3.6.
const char* const* input_pin_labels();
const char* const* output_pin_labels();

// ------------------------------------------------- the host build's stimulus ---
//
// Only the host HAL has these, and the guard is what says so: on a board an
// input line is a pin and nothing in software may drive it.

#if !defined(ARDUINO)

/// Drive the input word directly. The host build has no pins, so this is where
/// its inputs come from.
void set_native_inputs(LineBitmask word);

/// Wire every output line back to an input line, in software.
///
/// Output line *n* appears on input line *(n + shift) mod width*, which is the
/// loopback harness of docs/operations/hardware.md with the jumper wires taken out. It
/// exists so that the tests which drive a transition from a *predicate* --
/// the pin -> conditioner -> matches() -> transition chain -- are the same
/// tests with a board on the desk and without one. Before it, that chain could
/// only ever be exercised on silicon, so CI never ran it at all.
///
/// It is not pretending to be a board. The delay is whatever one scan of this
/// loop is rather than a propagation time, and nothing here has a deadline.
/// What it reproduces faithfully is the *logic*: which line a level arrives on,
/// and that it arrives one scan after it was raised.
///
/// `width` of 0 turns it off, which is the default -- a host device whose
/// inputs went high on their own would surprise every other test in the tree.
void set_native_loopback(uint8_t width, uint8_t shift);

#endif  // !ARDUINO

}  // namespace hal
}  // namespace statemachined
