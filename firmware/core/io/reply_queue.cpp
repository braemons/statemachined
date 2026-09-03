// SPDX-License-Identifier: GPL-3.0-or-later
#include "io/reply_queue.h"

namespace statemachined {

size_t ReplyQueue::pending() const {
  // One read of each index, in this order. The producer can only make this
  // answer stale by *adding*, so a consumer acting on it is conservative, and
  // the consumer's own reads of head_ are never stale to itself.
  const size_t tail = tail_;
  const size_t head = head_;
  return (tail >= head) ? tail - head : kReplyQueueBytes - head + tail;
}

bool ReplyQueue::push(const char* line, size_t n) {
  if (n == 0) return true;
  if (n > free_bytes()) return false;

  size_t at = tail_;
  for (size_t i = 0; i < n; ++i) {
    buf_[at] = line[i];
    if (++at == kReplyQueueBytes) at = 0;
  }
  // Published only now, and in one store: until this line runs the consumer
  // sees none of the bytes above, and after it runs it sees all of them.
  tail_ = at;
  return true;
}

size_t ReplyQueue::peek(const char** out) const {
  const size_t head = head_;
  const size_t tail = tail_;
  if (head == tail) return 0;
  // Up to the end of the buffer, so the caller writes from the array itself
  // rather than from a copy. A wrapped queue simply takes two passes.
  const size_t run = (tail > head) ? tail - head : kReplyQueueBytes - head;
  *out = buf_ + head;
  return run;
}

void ReplyQueue::consume(size_t n) {
  const size_t have = pending();
  if (n > have) n = have;  // a link claiming more than it was offered is a bug, not a crash
  size_t at = head_ + n;
  if (at >= kReplyQueueBytes) at -= kReplyQueueBytes;
  head_ = at;
}

void ReplyQueue::clear() {
  head_ = 0;
  tail_ = 0;
}

}  // namespace statemachined
