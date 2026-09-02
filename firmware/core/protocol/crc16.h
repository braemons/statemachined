// CRC-16/CCITT-FALSE, the check on every protocol line and the accumulator
// behind graph_end's and result_end's checksums.
//
// This is not security and is not claimed to be -- see dev/PROTOCOL.md, 1.1. It
// catches the failure that actually happens on a USB CDC link: a truncated or
// spliced line after a re-enumeration, caught early enough that a corrupt graph
// is refused rather than run.
#pragma once
#include <cstddef>
#include <cstdint>

namespace fsmd {

/// Polynomial 0x1021, initial value 0xFFFF, no reflection, no final XOR.
///
/// `seed` is what makes this an accumulator as well as a checksum: pass the
/// previous return value to continue over a stream, which is how graph_end's
/// checksum covers every message since graph_begin without buffering any of
/// them.
uint16_t crc16_ccitt(const void* data, size_t len, uint16_t seed = 0xFFFF);

/// The four uppercase hex digits the protocol puts on the wire. Writes exactly
/// four characters to `out` and does not terminate them.
void crc16_to_hex(uint16_t crc, char out[4]);

/// Reads four uppercase-or-lowercase hex digits. False if any is not a hex
/// digit -- a malformed CRC field is a bad line, not a zero CRC.
bool crc16_from_hex(const char in[4], uint16_t* out);

}  // namespace fsmd
