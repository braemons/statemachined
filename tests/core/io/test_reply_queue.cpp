// SPDX-License-Identifier: GPL-3.0-or-later
// The buffer that keeps a slow link from costing the scan its periods.
//
// Tested on the host rather than left in main.cpp, because a ring buffer that
// wraps wrongly corrupts a line rather than dropping one, and a corrupt line
// that still carries a valid CRC is the one failure the framing rules cannot
// catch.
#include <string>

#include "doctest.h"
#include "io/reply_queue.h"

using namespace statemachined;

namespace {

/// A link that accepts a fixed number of bytes per call, the way a USB endpoint
/// with a fifo does.
struct FakeLink {
  size_t per_call;
  std::string taken;

  size_t write_some(const char* p, size_t n) {
    const size_t take = n < per_call ? n : per_call;
    taken.append(p, take);
    return take;
  }
};

/// One drain pass: hand over whatever the link will take right now, and stop as
/// soon as it takes less than it was offered. This is the loop main.cpp runs.
void drain(ReplyQueue& q, FakeLink& link) {
  const char* p = nullptr;
  size_t n = 0;
  while ((n = q.peek(&p)) > 0) {
    const size_t wrote = link.write_some(p, n);
    q.consume(wrote);
    if (wrote < n) break;
  }
}

std::string line_of(char c, size_t n) { return std::string(n, c); }

}  // namespace

TEST_CASE("lines come out in the order they went in, byte for byte") {
  ReplyQueue q;
  FakeLink link{4};
  REQUIRE(q.push("hello\n", 6));
  REQUIRE(q.push("world\n", 6));
  while (!q.empty()) drain(q, link);
  CHECK(link.taken == "hello\nworld\n");
}

TEST_CASE("a line is queued whole or not at all") {
  // The caller is a ReplySink: told "no", it can drain and ask again, but told
  // "I took part of it" it has already put half a message on the wire.
  ReplyQueue q;
  const size_t room = q.free_bytes();
  const std::string big = line_of('x', room - 4);
  REQUIRE(q.push(big.data(), big.size()));
  CHECK_FALSE(q.push("12345", 5));
  CHECK(q.pending() == big.size());  // the refusal changed nothing

  CHECK(q.push("1234", 4));  // exactly the room left
  CHECK(q.free_bytes() == 0);
}

TEST_CASE("the ring wraps without reordering or corrupting a line") {
  // The case that matters: a queue that has been drained past the halfway mark
  // and then filled again, so the next line straddles the end of the buffer.
  ReplyQueue q;
  FakeLink link{67};  // an awkward size, so runs never line up with the wrap
  std::string expect;

  for (int i = 0; i < 200; ++i) {
    const std::string line = line_of(static_cast<char>('A' + (i % 26)), 100 + (i % 37));
    REQUIRE(q.push(line.data(), line.size()));
    expect += line;
    // Drained faster than it is filled, but never emptied, so the head keeps
    // advancing and a later line always straddles the end of the buffer.
    drain(q, link);
    drain(q, link);
  }
  while (!q.empty()) drain(q, link);
  CHECK(link.taken == expect);
}

TEST_CASE("peek offers a contiguous run, and two passes cover a wrap") {
  ReplyQueue q;
  // Leave the head 10 bytes from the end of the array, so the next line
  // straddles it.
  const std::string filler = line_of('a', kReplyQueueBytes - 10);
  REQUIRE(q.push(filler.data(), filler.size()));
  q.consume(filler.size());
  REQUIRE(q.empty());

  REQUIRE(q.push("0123456789ABCDEFGHIJ", 20));
  const char* p = nullptr;
  const size_t first = q.peek(&p);
  CHECK(first == 10);  // to the end of the array, no further
  CHECK(std::string(p, first) == "0123456789");
  q.consume(first);

  const size_t second = q.peek(&p);
  CHECK(second == 10);
  CHECK(std::string(p, second) == "ABCDEFGHIJ");
}

TEST_CASE("a link that takes nothing leaves the queue untouched") {
  ReplyQueue q;
  FakeLink dead{0};
  REQUIRE(q.push("pending\n", 8));
  drain(q, dead);
  CHECK(q.pending() == 8);
  CHECK(dead.taken.empty());
}

TEST_CASE("clear drops everything, for a link that went away") {
  // A reply from before the port closed must not surface after it reopens,
  // where it would answer a message_id belonging to a session that is gone.
  ReplyQueue q;
  REQUIRE(q.push("stale\n", 6));
  q.clear();
  CHECK(q.empty());
  CHECK(q.free_bytes() == kReplyQueueBytes - 1);

  FakeLink link{64};
  REQUIRE(q.push("fresh\n", 6));
  while (!q.empty()) drain(q, link);
  CHECK(link.taken == "fresh\n");
}

TEST_CASE("consume never runs past what is queued") {
  ReplyQueue q;
  REQUIRE(q.push("abc", 3));
  q.consume(99);
  CHECK(q.empty());
  CHECK(q.free_bytes() == kReplyQueueBytes - 1);
}

TEST_CASE("the longest line the protocol allows fits") {
  // Not arithmetic for its own sake: if kMaxLine ever grows past the queue, a
  // reply would be refused forever and the stall path would spin.
  ReplyQueue q;
  const std::string longest = line_of('m', kMaxLine);
  CHECK(q.push(longest.data(), longest.size()));
}

TEST_CASE("a producer interleaved with a consumer loses nothing and reorders nothing") {
  // The shape the firmware actually runs: the scan ISR pushes result messages
  // while the foreground is partway through handing an earlier line to the
  // link. Interrupt interleaving cannot be reproduced on the host, but the
  // ordering it can produce can: every push lands between two consumer steps,
  // which is exactly the set of points an interrupt could fall on.
  ReplyQueue q;
  FakeLink link{23};
  std::string expect;
  size_t produced = 0;

  for (int step = 0; step < 2000; ++step) {
    // Push at an interval that shares no factor with the drain size, so the
    // pushes land at every offset within the buffer over the run.
    if (step % 3 == 0) {
      const std::string line =
          line_of(static_cast<char>('a' + (produced % 26)), 40 + produced % 61);
      if (q.push(line.data(), line.size())) {
        expect += line;
        ++produced;
      }
    }
    // One drain step, the way one pass of loop() does it.
    const char* p = nullptr;
    const size_t n = q.peek(&p);
    if (n > 0) q.consume(link.write_some(p, n));
  }
  while (!q.empty()) drain(q, link);

  CHECK(produced > 100);  // the run actually exercised something
  CHECK(link.taken == expect);
}
