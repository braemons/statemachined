// SPDX-License-Identifier: GPL-3.0-or-later
// The host. Not a board, and not a toy either: it is what lets a whole session
// be played out at host speed with no hardware attached, and it is why hal.h is
// compiled by CI rather than only by a board build.
//
// Input lines are driven by set_native_inputs() rather than read from anywhere,
// so a test or a simulator drives the rig. Output lines are recorded. The clock
// is the monotonic one, truncated to 32 bits exactly as a board's is -- if
// something breaks on the wrap every ~71 minutes, it should break here too.
#if !defined(ARDUINO)

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>

#include <cstdio>
#include <cstdlib>
#include <ctime>

#include "hal.h"

namespace statemachined {
namespace hal {
namespace {
LineBitmask inputs_ = 0;
LineBitmask levels_ = 0;

// The software loopback harness; see set_native_loopback() in hal.h. Width 0 is
// off, and off is the default -- a host device whose inputs rose on their own
// would surprise every test that never asked for this.
uint8_t loopback_width_ = 0;
uint8_t loopback_shift_ = 0;

/// The listening socket and the one host on it, or -1 for none.
int listener_ = -1;
int host_ = -1;

void make_nonblocking(int fd) {
  const int flags = fcntl(fd, F_GETFL, 0);
  if (flags >= 0) fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

/// Take a waiting host if there is none. Never blocks: the listener is
/// non-blocking, like everything else a scan loop touches.
void accept_a_host() {
  if (host_ >= 0 || listener_ < 0) return;
  const int fd = ::accept(listener_, nullptr, nullptr);
  if (fd < 0) return;
  make_nonblocking(fd);
  const int one = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  host_ = fd;
}

void drop_the_host() {
  if (host_ >= 0) ::close(host_);
  host_ = -1;
}
}  // namespace

// No pins, and the labels say so rather than borrowing a board's.
//
// A host build could plausibly answer "D2" here and let the integration tests
// look more like a rig. It would be a lie of exactly the kind the `pins`
// command exists to prevent: nothing here is wired to anything. "sim0" is
// honest, and it still exercises the whole path -- a host that resolves a
// config's `pin = "sim0"` against this device is doing precisely what it does
// against a board, which is asking rather than assuming.
constexpr const char* kSimulatedLineLabels[] = {
    "sim0",  "sim1",  "sim2",  "sim3",  "sim4",  "sim5",  "sim6",  "sim7",
    "sim8",  "sim9",  "sim10", "sim11", "sim12", "sim13", "sim14", "sim15",
    "sim16", "sim17", "sim18", "sim19", "sim20", "sim21", "sim22", "sim23",
    "sim24", "sim25", "sim26", "sim27", "sim28", "sim29", "sim30", "sim31",
};

// One table for both directions: input line 3 and output line 3 are different
// things on a board and neither of them is here, so inventing two sets of names
// would only suggest otherwise.
static_assert(sizeof(kSimulatedLineLabels) / sizeof(kSimulatedLineLabels[0]) >= kMaxLines,
              "every input line this build reports needs a label");
static_assert(sizeof(kSimulatedLineLabels) / sizeof(kSimulatedLineLabels[0]) >= kMaxOutputLines,
              "every output line this build reports needs a label");

const char* const* input_pin_labels() { return kSimulatedLineLabels; }
const char* const* output_pin_labels() { return kSimulatedLineLabels; }

void init() {
  levels_ = 0;
  inputs_ = 0;
  // Deliberately *not* clearing the loopback: it is configured before init()
  // by whoever built this device, and it describes the wiring rather than the
  // state. A board's jumper wires survive a reset too.
}

uint16_t native_listen(uint16_t port) {
  listener_ = ::socket(AF_INET, SOCK_STREAM, 0);
  if (listener_ < 0) return 0;
  const int one = 1;
  setsockopt(listener_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  address.sin_port = htons(port);
  if (::bind(listener_, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0 ||
      ::listen(listener_, 1) != 0) {
    ::close(listener_);
    listener_ = -1;
    return 0;
  }
  // hal.h promises link_read() never blocks, and on a board that is free --
  // asking a UART peripheral whether a byte is waiting cannot block. Here it
  // has to be arranged: a blocking accept or read would stall the scan loop
  // while the trial it is meant to be advancing stood still.
  make_nonblocking(listener_);
  socklen_t length = sizeof(address);
  getsockname(listener_, reinterpret_cast<sockaddr*>(&address), &length);
  return ntohs(address.sin_port);
}

LineBitmask read_inputs() {
  if (loopback_width_ == 0) return inputs_;
  // Whatever was driven directly, plus whatever the outputs are feeding back.
  // Both, rather than only the loopback, so a test can still hold a line high
  // by hand on a device that also has the harness on.
  LineBitmask word = inputs_;
  for (uint8_t out = 0; out < loopback_width_; ++out) {
    if (levels_ & (static_cast<LineBitmask>(1) << out)) {
      word |= static_cast<LineBitmask>(1) << ((out + loopback_shift_) % loopback_width_);
    }
  }
  return word;
}

void write_outputs(LineBitmask set_high, LineBitmask set_low) {
  // Same rule as the board: a line in both is a bug upstream, and it goes high.
  levels_ = (levels_ | set_high) & ~(set_low & ~set_high);
}

Microseconds micros_now() {
  timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  const unsigned long long us =
      static_cast<unsigned long long>(ts.tv_sec) * 1000000ull + ts.tv_nsec / 1000;
  return static_cast<Microseconds>(us);  // truncated, so the wrap is real here too
}

size_t link_read(char* dst, size_t max) {
  accept_a_host();
  if (max == 0 || host_ < 0) return 0;
  const ssize_t n = ::recv(host_, dst, max, 0);
  if (n > 0) return static_cast<size_t>(n);
  // Zero is the host closing the connection, and anything but "nothing
  // waiting" is the connection gone: either way link_up() now says so, and the
  // device fails safe exactly as a board whose port closed does.
  if (n == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) drop_the_host();
  return 0;
}

size_t link_write_some(const char* src, size_t n) {
  // With nobody connected, what the device says goes nowhere -- which is what
  // link loss *is*.
  if (host_ < 0) return n;
  const ssize_t wrote = ::send(host_, src, n, MSG_NOSIGNAL);
  if (wrote >= 0) return static_cast<size_t>(wrote);
  if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) return 0;
  drop_the_host();
  return n;
}

bool link_up() {
  accept_a_host();
  return host_ >= 0;
}

// -------------------------------------------------------------- the store ---
//
// A file, so that the host behaves like a board in the one way that matters
// here: settings written by one run are there for the next. Named by
// STATEMACHINED_STORE if the environment says so, which is what lets two native
// devices in one test run not share a store; otherwise a file in the working
// directory.
//
// The whole store is held in memory, which would be indefensible on the board
// and is nothing on a host: 8 KB, the size of the RA4M1's data flash, so that a
// record that would not fit there does not fit here either.

namespace {

constexpr size_t kStoreBytes = 8192;
uint8_t store_[kStoreBytes];
bool store_loaded_ = false;
size_t write_at_ = 0;
bool write_failed_ = false;

const char* store_path() {
  const char* p = std::getenv("STATEMACHINED_STORE");
  return (p != nullptr && p[0] != '\0') ? p : "statemachined-store.bin";
}

/// Erased flash reads as 0xFF, and so does an absent file. That is what makes a
/// board nobody has saved to report BadMagic rather than something worse.
void load_store() {
  if (store_loaded_) return;
  store_loaded_ = true;
  for (size_t i = 0; i < kStoreBytes; ++i) store_[i] = 0xFF;
  std::FILE* f = std::fopen(store_path(), "rb");
  if (f == nullptr) return;
  // A short read is not an error here, and the count is deliberately used
  // rather than ignored: a file smaller than the store -- or one truncated by
  // the power going during a save -- leaves the rest of the array at 0xFF,
  // which is exactly what the unwritten part of a real erased sector reads as.
  const size_t got = std::fread(store_, 1, kStoreBytes, f);
  (void)got;
  std::fclose(f);
}

}  // namespace

size_t storage_capacity() { return kStoreBytes; }

bool storage_read(size_t offset, void* dst, size_t n) {
  load_store();
  if (offset + n > kStoreBytes) return false;
  for (size_t i = 0; i < n; ++i) static_cast<uint8_t*>(dst)[i] = store_[offset + i];
  return true;
}

bool storage_write_begin() {
  load_store();
  for (size_t i = 0; i < kStoreBytes; ++i) store_[i] = 0xFF;  // the erase
  write_at_ = 0;
  write_failed_ = false;
  return true;
}

bool storage_write(const void* src, size_t n) {
  if (write_failed_) return false;
  if (write_at_ + n > kStoreBytes) {
    write_failed_ = true;
    return false;
  }
  for (size_t i = 0; i < n; ++i) store_[write_at_ + i] = static_cast<const uint8_t*>(src)[i];
  write_at_ += n;
  return true;
}

bool storage_write_commit() {
  if (write_failed_) return false;
  std::FILE* f = std::fopen(store_path(), "wb");
  if (f == nullptr) return false;
  const size_t wrote = std::fwrite(store_, 1, kStoreBytes, f);
  std::fclose(f);
  return wrote == kStoreBytes;
}

// ------------------------------------------------------ the rig, simulated ---

void set_native_inputs(LineBitmask word) { inputs_ = word; }

void set_native_loopback(uint8_t width, uint8_t shift) {
  loopback_width_ = width;
  loopback_shift_ = width == 0 ? 0 : static_cast<uint8_t>(shift % width);
}
LineBitmask native_output_levels() { return levels_; }

}  // namespace hal
}  // namespace statemachined

#endif  // !ARDUINO
