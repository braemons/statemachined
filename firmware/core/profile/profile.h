// SPDX-License-Identifier: GPL-3.0-or-later
// Where the board's time goes, measured in cycles, in a build that asks for it.
//
// firmware/src/main.cpp explains why a command costs the scan: the foreground
// holds the engine while the session handles the frame, and every scan period
// that goes by in that window is an overrun. What it cannot say is *which part*
// of handling a frame the time goes into -- the frame reader, nanopb, the
// handler, the reply -- and tests/perf can only see the total from outside.
// This is the inside view.
//
// Compiled in only with -DSTATEMACHINED_PROFILE (the `uno_r4_minima_profile`
// env). Without it every macro below is empty and the build is byte-identical
// to one that never heard of this file; with it, each measured region costs two
// reads of the cycle counter. A `profile` command (link.proto) reads the
// figures out and may reset them; a build without profiling answers it with
// `enabled: false` rather than refusing, so the perf suite can say which build
// it is talking to.
//
// **Spans nest, and the report does not subtract.** `dispatch` includes the
// `compose`, `encode` and `frame_write` of the reply it sends; `hold` includes
// everything the session does with a frame. Each is the wall time of its own
// region, which is what makes a nested breakdown readable at all: the host
// subtracts, knowing the tree.
//
// Written from two contexts. The scan spans are the ISR's alone and every
// other span is the foreground's, so no two writers ever share a record. A
// foreground read of a scan span can tear against the ISR -- it is a profile,
// read by a person, and the next read is right.
#pragma once

#include <cstdint>

namespace statemachined {
namespace profile {

/// The regions measured. The numbering is the wire's (`ProfileSpan.span` in
/// link.proto); a new one goes at the end.
enum class Span : uint8_t {
  ScanIdle = 0,        ///< one scan, no trial running (ISR)
  ScanTrial = 1,       ///< one scan, a trial running (ISR)
  HoldIdle = 2,        ///< the engine held for a frame, no trial running
  HoldTrial = 3,       ///< the same, a trial running: the one that matters
  LinkRead = 4,        ///< hal::link_read, outside the hold
  FrameRead = 5,       ///< the frame reader: COBS, CRC, per byte (count is bytes)
  RxReset = 6,         ///< unused since decode clears only its own body
  Decode = 7,          ///< pb_decode of one HostMessage
  Dispatch = 8,        ///< the handler, reply included
  Compose = 9,         ///< selecting and clearing a reply's body
  Encode = 10,         ///< pb_encode of one DeviceMessage
  FrameWrite = 11,     ///< COBS and CRC around an encoded reply
  DrainOutbound = 12,  ///< visits and results, built in loop()
  DrainTx = 13,        ///< handing queued bytes to the USB stack
  Count
};

struct SpanStats {
  uint32_t count = 0;
  uint64_t total_cycles = 0;
  uint32_t max_cycles = 0;
};

struct Profile {
  SpanStats spans[static_cast<uint8_t>(Span::Count)];
  /// Overruns counted while a trial was running: the ones that cost an
  /// experiment something. Kept here rather than in ScanHealth because only a
  /// profiling build splits them.
  uint32_t overruns_in_trial = 0;
  uint32_t worst_gap_in_trial = 0;
};

#if defined(STATEMACHINED_PROFILE)

constexpr bool kEnabled = true;

/// The one profile, in the core so the session can report it.
Profile& get();
void reset();

/// A free-running cycle counter and its rate. The board's HAL installs one in
/// hal::init() (the DWT counter on a Cortex-M; nanoseconds on the host). Until
/// one does -- the core's host tests link no HAL -- the counter counts its own
/// reads, which keeps the plumbing testable without pretending to be time.
using Clock = uint32_t (*)();
void set_clock(Clock clock, uint32_t per_second);
uint32_t cycles();
uint32_t cycles_per_second();

inline void record(Span span, uint32_t elapsed) {
  SpanStats& s = get().spans[static_cast<uint8_t>(span)];
  ++s.count;
  s.total_cycles += elapsed;
  if (elapsed > s.max_cycles) s.max_cycles = elapsed;
}

/// Times its own lifetime into `span`.
class Scope {
 public:
  explicit Scope(Span span) : span_(span), start_(cycles()) {}
  ~Scope() { record(span_, cycles() - start_); }

 private:
  Span span_;
  uint32_t start_;
};

#define STATEMACHINED_PROFILE_CAT2(a, b) a##b
#define STATEMACHINED_PROFILE_CAT(a, b) STATEMACHINED_PROFILE_CAT2(a, b)
/// Time the rest of the enclosing block.
#define PROFILE_SCOPE(span)                                                       \
  const ::statemachined::profile::Scope STATEMACHINED_PROFILE_CAT(profile_scope_, \
                                                                  __LINE__)(span)
/// A region that is not a block: remember a start, record an end.
#define PROFILE_START(name) const uint32_t name = ::statemachined::profile::cycles()
#define PROFILE_END(span, name) \
  ::statemachined::profile::record(span, ::statemachined::profile::cycles() - (name))

#else

constexpr bool kEnabled = false;

#define PROFILE_SCOPE(span) static_cast<void>(0)
#define PROFILE_START(name) static_cast<void>(0)
#define PROFILE_END(span, name) static_cast<void>(0)

#endif

}  // namespace profile
}  // namespace statemachined
