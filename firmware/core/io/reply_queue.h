// SPDX-License-Identifier: GPL-3.0-or-later
// Lines waiting for a link that is not ready to take them yet.
//
// This exists because of a measurement. The scan runs in loop() and the timer
// only counts ticks (firmware/src/main.cpp says why), so anything that blocks
// the foreground costs scans -- and on the reference board, writing a reply
// blocked it badly: the Renesas core's USB CDC write() spins until TinyUSB has
// accepted every byte, which means waiting for the host to poll the endpoint.
// Measured on hardware at 10 kHz: ~10 missed scan periods for a 65-byte reply
// and ~16 for a 290-byte one, or roughly a 0.7 ms fixed wait plus 2.5 us per
// byte, every single command.
//
// So replies go in here instead, and the foreground hands the link only as much
// as it will take right now. A reply then costs the scan nothing: it goes out
// over the following passes, 100 us apart, while the trial keeps being scanned
// on time.
//
// What the queue does NOT do is reorder or split a line. A line is pushed whole
// or not at all, and they leave in the order they arrived: half a `graph_state`
// on the wire is exactly the failure the framing rules exist to prevent, and a
// result chunk overtaking its `result_begin` would break the rolling checksum
// that catches a dropped one.
//
// ---------------------------------------------------------------------------
// One producer, one consumer, no critical section.
// ---------------------------------------------------------------------------
//
// The scan runs in the timer ISR and the link is drained in the foreground, so
// a result message is pushed from interrupt context while `peek`/`consume` are
// running outside it. That is safe here without disabling anything, and the
// reason is structural rather than lucky:
//
//   * `head_` is written only by the consumer, `tail_` only by the producer.
//     There is no shared count for both to update -- which is exactly why this
//     class does not keep one.
//   * Each side publishes with a single aligned 32-bit store, after the bytes
//     it describes are already in place. A reader therefore sees either the old
//     index or the new one, never a half-written one, and never data that has
//     not been written yet.
//   * One slot is left empty so that full and empty are distinguishable without
//     a count. That is where the missing byte of capacity goes.
//
// The pairing this relies on: pushes from the foreground happen only while the
// ISR is deferring (see the guard in firmware/src/main.cpp), so there is never
// more than one producer at a time.
#pragma once
#include <cstddef>
#include <cstdint>

#include "config.h"

namespace statemachined {

/// Room for a couple of the longest lines the protocol allows.
///
/// Sized for request/response, which is what the link does in a trial's
/// critical path: one command in flight, one reply. It is deliberately *not*
/// sized for the burst that ends a trial -- result_begin, its path chunks and
/// result_end can run to several KB -- because that burst happens when the
/// trial is already over and jitter costs nothing, and carrying KB of buffer
/// for it would take the RAM out of the graph pools that need it.
constexpr uint16_t kReplyQueueBytes = 2 * kMaxLine;

class ReplyQueue {
 public:
  /// Queue one complete line, or refuse it and change nothing. Producer side.
  ///
  /// All-or-nothing on purpose: a caller told "no" can drain and try again,
  /// whereas a caller told "I took 300 of your 400 bytes" has already put a
  /// truncated line on the wire.
  bool push(const char* line, size_t n);

  /// The next run of bytes to hand the link, which is at most up to the end of
  /// the buffer -- a wrapped queue needs two calls, and that is the caller's
  /// loop, not a memmove here. Consumer side.
  size_t peek(const char** out) const;

  /// Drop the `n` bytes the link accepted. Never more than peek() offered.
  /// Consumer side.
  void consume(size_t n);

  /// Everything queued, discarded. For link loss: there is nobody to read
  /// these, and a reply from before the port closed must not surface after it
  /// reopens, where it would answer a message_id from a session that is gone.
  ///
  /// Touches both indices, so unlike the rest of this class it is only safe
  /// with the producer held off -- main.cpp calls it under the same guard that
  /// stops the ISR scanning.
  void clear();

  size_t pending() const;
  bool empty() const { return pending() == 0; }
  /// One less than the array, because the empty slot is what tells full from
  /// empty without a shared counter.
  size_t free_bytes() const { return kReplyQueueBytes - 1 - pending(); }

 private:
  char buf_[kReplyQueueBytes];
  /// Where the next byte leaves. Written only by the consumer.
  volatile size_t head_ = 0;
  /// Where the next byte arrives. Written only by the producer.
  volatile size_t tail_ = 0;
};

}  // namespace statemachined
