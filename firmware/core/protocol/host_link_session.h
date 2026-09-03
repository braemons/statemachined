// SPDX-License-Identifier: GPL-3.0-or-later
// The device's side of the conversation with the host bridge.
//
// A *session* is everything that persists between messages: `hello` opens one,
// carrying the seed every trial's random stream is derived from, and everything
// until the next `hello` belongs to it -- the committed graph, which trial is
// armed, what was last answered. This is the only object that knows a `start`
// is legal because a `configure` arrived earlier, which is what makes "no trial
// runs that the device was not confirmed configured for" enforceable in one
// place rather than hoped for in several.
//
// It touches no port. Bytes arrive through receive(), replies leave through a
// ReplySink, and the trial loop is driven by advance_trial() from wherever the
// timer lives. That separation is what lets a whole session be played out on
// the host, at host speed, with no board attached.
//
// See dev/PROTOCOL.md.
#pragma once
#include <cstddef>
#include <cstdint>

#include "config.h"
#include "graph/state_graph.h"
#include "protocol/framing.h"
#include "protocol/graph_builder.h"
#include "protocol/json.h"
#include "trial/trial_runner.h"

namespace fsmd {

/// Where a reply goes. Abstract because the session must not know whether the
/// far end is a USB CDC endpoint or a std::string in a test.
class ReplySink {
 public:
  virtual ~ReplySink() = default;
  /// One complete line, newline included. Never called with a partial line.
  virtual void send_line(const char* bytes, size_t n) = 0;
};

/// What the device says about itself in `hello_ack`, so the bridge can check a
/// graph against this board's capacities before uploading a byte of it.
struct DeviceIdentity {
  const char* board = "native";
  const char* firmware_version = "0.1.0";
  uint8_t input_line_count = kMaxLines;
  uint8_t output_line_count = kMaxOutputLines;
  /// Measured at boot on a real board, not declared, so the host learns the
  /// timing resolution it is actually getting rather than the hoped-for one.
  uint32_t measured_scan_hz = 0;
};

/// How well the board is keeping to its scan period, reported to the host in
/// `state_report`.
///
/// The scan rate is a claim until a board runs it, and a board that quietly
/// misses scans looks exactly like a board that is fine. So a missed scan is
/// counted rather than absorbed: whoever is debugging a rig can see that a
/// response window was measured on a clock that stuttered.
struct ScanHealth {
  uint32_t hz = 0;         ///< measured at boot, not declared
  uint32_t overruns = 0;   ///< scan periods that elapsed with no scan in them
  uint32_t worst_gap = 0;  ///< the most periods ever missed in a row
};

/// Makes a retried command idempotent.
///
/// The bridge sends a command, times out waiting, and resends. Acting on it
/// twice is the failure that matters -- a re-executed `start` runs a second
/// trial -- so a command whose `seq` matches the one last answered gets that
/// same answer back, byte for byte, and changes nothing.
///
/// One command deep, not a window: this is strict request/response with one
/// command in flight, and remembering more would mean storing that many
/// complete replies at kMaxLine bytes each.
class DuplicateCommandGuard {
 public:
  bool is_repeat(uint16_t seq) const { return have_ && seq == last_seq_; }

  const char* reply() const { return reply_; }
  size_t reply_len() const { return reply_len_; }

  void remember(uint16_t seq, const char* line, size_t n);
  void forget() { have_ = false; }

 private:
  char reply_[kMaxLine];
  size_t reply_len_ = 0;
  uint16_t last_seq_ = 0;
  bool have_ = false;
};

/// What the session is doing, which is what decides whether a command is legal.
enum class LinkState : uint8_t {
  Greeting = 0,  ///< no hello yet; only hello is accepted
  Idle,          ///< greeted. A graph may or may not be committed
  Armed,         ///< configured for a trial that has not started
  Running,       ///< a trial is in flight
};

class HostLinkSession {
 public:
  HostLinkSession(ReplySink& out, const DeviceIdentity& identity);

  /// Feed bytes from the host, in whatever chunks arrived. Complete lines are
  /// handled as they complete, and replies go out through the sink before this
  /// returns.
  void receive(const char* bytes, size_t n, Microseconds now_us);
  void receive_byte(char c, Microseconds now_us);

  /// One tick of the trial loop, driven by the timer rather than by the link.
  /// Emits the chunked result when the trial ends, so the host learns of an
  /// outcome without having to ask.
  OutputUpdate advance_trial(LineBitmask word, Microseconds now_us);

  /// The host is gone -- the port closed, or the heartbeat lapsed. Cancels a
  /// trial in flight as `link_lost` and returns the outputs that owes, which
  /// the caller should apply along with fail_safe().
  ///
  /// A cancel rather than a stop: the trial ends through the ordinary exit
  /// path, so every line the current state raised comes down by the same code
  /// that lowers it on any other transition. There is nobody to send the result
  /// to, which is exactly why the outputs cannot be left to the host to sort
  /// out.
  OutputUpdate link_lost(Microseconds now_us);

  /// Drive every output to its configured safe level. Called on link loss, on a
  /// refused graph and at reset -- "off" is not always "low", so this is data
  /// rather than a zeroed word.
  ///
  /// Not const: it also tells the engine where the pins now are, so a Toggle
  /// after a fail-safe goes the right way. Nothing can read a pin back.
  OutputUpdate fail_safe();

  LinkState state() const { return state_; }
  bool has_graph() const { return have_graph_; }
  uint16_t graph_version() const { return live_graph_.version; }
  const StateGraph& graph() const { return live_graph_; }
  uint32_t armed_trial_id() const { return armed_trial_id_; }

  /// Tell the session how the scan loop is doing, for state_report. Set by
  /// whatever owns the timer -- the session cannot see its own lateness.
  void report_scan_health(const ScanHealth& h) { scan_ = h; }

  /// The conditioned input word as of the last scan, and what the engine
  /// believes it has driven the outputs to. Reported in state_report, which is
  /// the only way anything outside the device can check that a graph's line
  /// numbers reach the pins somebody wired -- there is no read-back path.
  LineBitmask input_word() const { return last_word_; }
  LineBitmask output_word() const { return runner_.driven_levels(); }

  /// Lines the link layer threw away, for state_report. A link dropping lines
  /// should be visible to whoever is debugging the rig rather than inferred
  /// from trials that did not happen.
  uint32_t dropped_lines() const { return reader_.dropped(); }

 private:
  void handle_line(const char* line, size_t n, Microseconds now_us);
  void dispatch(const JsonObject& m, JsonSpan covered, uint16_t seq, Microseconds now_us);

  // One handler per host command. Each is responsible for sending exactly one
  // reply, which is what makes a retry decidable for the bridge.
  void on_hello(const JsonObject& m, uint16_t seq);
  void on_graph_message(const JsonObject& m, JsonSpan covered, uint16_t seq, JsonSpan type);
  void on_configure(const JsonObject& m, uint16_t seq);
  void on_start(const JsonObject& m, uint16_t seq, Microseconds now_us);
  void on_cancel(const JsonObject& m, uint16_t seq, Microseconds now_us);
  void on_ping(uint16_t seq, Microseconds now_us);
  void on_state_request(uint16_t seq, Microseconds now_us);

  void send_error(uint16_t seq, const char* code, const char* message, const char* context);
  void send_ack(uint16_t seq);
  void emit_result();

  /// Finish, frame and send whatever is in the transmit buffer, remembering it
  /// as the answer to `seq` so a retry can be answered from the cache.
  void send(JsonWriter& w, uint16_t seq);
  void send_unsolicited(JsonWriter& w);

  ReplySink& out_;
  DeviceIdentity identity_;

  char rx_[kMaxLine];
  LineReader reader_;
  char tx_[kMaxLine];

  GraphBuilder builder_;
  StateGraph live_graph_;
  bool have_graph_ = false;

  TrialRunner runner_;
  LinkState state_ = LinkState::Greeting;

  uint64_t session_seed_ = 0;
  uint32_t armed_trial_id_ = 0;
  bool start_from_serial_ = true;

  DuplicateCommandGuard guard_;
  uint16_t tx_seq_ = 0;
  Microseconds booted_us_ = 0;
  bool have_boot_ = false;
  uint32_t bad_lines_ = 0;
  ScanHealth scan_;
  LineBitmask last_word_ = 0;

  /// Outputs owed by a start() that happened between scans. The entry state's
  /// actions are returned by TrialRunner::start(), which is called from the
  /// link, and the only thing that drives pins is advance_trial(), which is
  /// called from the timer. Without this they are returned to nobody.
  OutputUpdate pending_ops_;
};

}  // namespace fsmd
