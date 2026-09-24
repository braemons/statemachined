// SPDX-License-Identifier: GPL-3.0-or-later
// CRC-16/CCITT-FALSE, the check on every protocol frame and the accumulator
// behind set_end's and result_end's checksums.
//
// This is not security and is not claimed to be -- see docs/reference/protocol.md, 1.1. It
// catches the failure that actually happens on a USB CDC link: a truncated or
// spliced frame after a re-enumeration, caught early enough that a corrupt graph
// is refused rather than run.
#pragma once
#include <cstddef>
#include <cstdint>

namespace statemachined {

/// Polynomial 0x1021, initial value 0xFFFF, no reflection, no final XOR.
///
/// `seed` is what makes this an accumulator as well as a checksum: pass the
/// previous return value to continue over a stream, which is how graph_end's
/// checksum covers every message since graph_begin without buffering any of
/// them.
uint16_t crc16_ccitt(const void* data, size_t len, uint16_t seed = 0xFFFF);

}  // namespace statemachined
