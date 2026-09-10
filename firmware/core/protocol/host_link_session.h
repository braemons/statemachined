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
// See docs/reference/protocol.md.
#pragma once
#include <cstddef>
#include <cstdint>

#include "config.h"
#include "graph/graph_set.h"
#include "io/settings_store.h"
#include "io/wiring.h"
#include "machine/global_timers.h"
#include "protocol/framing.h"
#include "protocol/graph_builder.h"
#include "protocol/json.h"
#include "protocol/msg_type.h"
#include "trial/autorun.h"
#include "trial/trial_runner.h"

namespace statemachined {

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

  /// What is written on the board beside each line, indexed by line number:
  /// `input_pin_labels[3]` is the label of input line 3. Answered to the host
  /// by the `pins` command (docs/reference/protocol.md 3.6).
  ///
  /// **This is the table that drives pinMode(), not a copy of it.** A host
  /// cannot otherwise know which pin a line is, or even which lines are inputs:
  /// the direction is fixed when this firmware is compiled and no command
  /// changes it. Before `pins` existed the host had to keep its own copy of
  /// this table keyed by the board name -- which is a hand-copied pin map, and
  /// a hand-copied pin map is the silent wrong-valve bug the HAL refuses to
  /// have for exactly the same reason.
  ///
  /// nullptr means this build has no pins worth naming, and `pins` is then
  /// refused with `no_pin_map` rather than answered with something invented.
  const char* const* input_pin_labels = nullptr;
  const char* const* output_pin_labels = nullptr;
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
  /// Times the reply queue was full and the foreground had to wait for the link
  /// before it could hand over a line. Replies are queued and drained without
  /// blocking, so this is the one remaining way the link can cost the scan its
  /// periods -- and a burst big enough to do it means the queue is smaller than
  /// this board's traffic, which is a number rather than a guess.
  uint32_t tx_stalls = 0;
};

/// Makes a retried command idempotent.
///
/// The bridge sends a command, times out waiting, and resends. Acting on it
/// twice is the failure that matters -- a re-executed `start` runs a second
/// trial -- so a command whose `message_id` matches the one last answered gets
/// that same answer back, byte for byte, and changes nothing.
///
/// One command deep, not a window: this is strict request/response with one
/// command in flight, and remembering more would mean storing that many
/// complete replies at kMaxLine bytes each.
class DuplicateCommandGuard {
 public:
  bool is_repeat(uint16_t message_id) const { return have_ && message_id == last_message_id_; }

  const char* reply() const { return reply_; }
  size_t reply_len() const { return reply_len_; }

  void remember(uint16_t message_id, const char* line, size_t n);
  void forget() { have_ = false; }

 private:
  char reply_[kMaxLine];
  size_t reply_len_ = 0;
  uint16_t last_message_id_ = 0;
  bool have_ = false;
};

/// One distribution's parameters as they were before a trial patched them.
///
/// `configure` may override a, b and c for the trial it arms, and the override
/// is reverted when that trial ends. What is stored is therefore not the patch
/// but its *inverse*: the values to put back. Keeping the originals rather than
/// re-uploading the set afterwards is the whole reason a patch is cheap enough
/// to be in a trial's critical path.
struct PatchedDistribution {
  RandomDistributionIndex index = kNoRandomDistribution;
  Milliseconds a = 0;
  Milliseconds b = 0;
  Milliseconds c = 0;
};

/// What the session is doing, which is what decides whether a command is legal.
enum class LinkState : uint8_t {
  Greeting = 0,  ///< no hello yet; only hello is accepted
  Idle,          ///< greeted. A graph may or may not be committed
  Armed,         ///< configured for a trial that has not started
  Running,       ///< a trial is in flight
  /// A run has ended and the device is counting down the dwell its terminal
  /// state declared before starting another on its own. Only reachable under
  /// autorun; a host-driven session goes back to Idle instead.
  Relighting,
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

  /// Serialise and send whatever visits advance_trial() has recorded since the
  /// last call. **Belongs in the foreground, next to the other link work.**
  ///
  /// This used to happen inside advance_trial() itself, which on a board means
  /// inside the scan ISR -- and building a ~130 byte JSON line with a CRC on it
  /// costs about 120 us there. That does not merely add jitter: the scan
  /// overruns its own tick, so the *next* scan lands late, and the state
  /// machine's response to a line went from one scan period to 220 us. Measured
  /// both ways on an Uno R4 Minima: 222 us with the stream on the ISR, exactly
  /// 100 us with it off, and insensitive to how much else the entry action did.
  /// See docs/operations/hardware.md, "Response latency and duration accuracy".
  ///
  /// It also put ReplySink::send_line() -- which spins when the transmit queue
  /// is full -- on the interrupt, which is the one context its own contract
  /// says it must not be on. Both go away by moving the formatting here: the
  /// ISR copies sixteen bytes into a ring and returns.
  /// Send whatever the trial loop has produced and not yet put on the wire:
  /// the visits, then the result of a run that has ended. **The one call the
  /// foreground owes the device every pass.**
  ///
  /// Everything that formats a line lives behind this, and that is the point.
  /// `advance_trial()` is the timer interrupt on a board; ReplySink::send_line()
  /// spins when the transmit queue is full; and a spin inside an interrupt,
  /// waiting on the foreground that drains that queue, does not end. Keeping
  /// both the visit stream and the result out here is what makes that
  /// unreachable rather than unlikely -- and it is what took the board's answer
  /// to a line from 220 us to one scan period. See drain_visits().
  ///
  /// `max_visit_lines` paces the visit half; the result follows once the ring
  /// is empty, however many passes that takes.
  void drain_outbound(uint8_t max_visit_lines = 0);

  /// `max_lines` of 0 means "everything waiting". Pass a small number from a
  /// loop that also pushes bytes at the link: emptying a full ring in one pass
  /// hands the transmit queue more lines than it holds, and ReplySink::send_line
  /// then spins waiting for the wire -- 21 such stalls on the reference board,
  /// where the budget for them is zero. One per pass, interleaved with the
  /// drain, moves the same lines with none.
  void drain_visits(uint8_t max_lines = 0);

  /// Visits the ring had no room for. Reported in `state_report` beside the
  /// scan's own health, because it is the same question -- is this device
  /// keeping up -- and the host can already see the gap in `seq`.
  uint32_t dropped_visits() const { return dropped_visits_; }

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

  /// This board's wiring, and whether anybody has set it. Until a `wiring`
  /// command arrives it is the compile-time default (config.h,
  /// STATEMACHINED_SAFE_LEVELS), which is what makes the fail_safe() before the
  /// first scan correct on a rig nobody has greeted yet.
  const DeviceWiring& wiring() const { return wiring_; }
  bool has_wiring() const { return have_wiring_; }

  /// Bumped every time the wiring changes, so whoever owns the InputConditioner
  /// can tell that it needs reconfiguring without comparing 72 bytes every
  /// scan. The session cannot reach the conditioner itself: it touches no pin.
  uint16_t wiring_revision() const { return wiring_revision_; }

  /// Install a wiring from inside the firmware rather than from the host --
  /// what restore_settings() uses for a wiring read back from storage. Counts
  /// as "has_wiring" only if `from_host`, since hello_ack's job is to tell the
  /// daemon whether the board was configured, not whether a default was
  /// applied, and a wiring the board remembered about itself is not the daemon
  /// having configured it.
  void set_wiring(const DeviceWiring& w, bool from_host = false);

  LinkState state() const { return state_; }

  /// The stored autorun settings, and whether the device is acting on them.
  ///
  /// The two are separate because a host that greets takes the rig: the setting
  /// survives a takeover so that the next boot still comes up self-driving,
  /// while the driving itself stops the moment somebody is there to do it. See
  /// trial/autorun.h.
  const AutorunConfig& autorun() const { return autorun_; }
  bool autorun_active() const { return autorun_active_; }

  /// Start driving trials, with no host and no `hello` -- what a board that
  /// restored its settings from storage does at boot, and the one path into
  /// this that does not come off the wire.
  ///
  /// Refused, and returns false, if no graph set is committed or the graph
  /// index names no graph: a board that armed itself against a set it does not
  /// have would be a rig running nothing while claiming to run something.
  ///
  /// The first run begins on the next advance_trial() rather than here, because
  /// this is not the scan's thread of control and nothing else in this class
  /// drives a pin from anywhere else.
  bool begin_autorun(const AutorunConfig& c, Microseconds now_us);

  /// Stop driving trials, cancelling one in flight through the ordinary exit
  /// path. The stored settings are untouched -- this says "not now", not "not
  /// ever". Outputs are owed to the next advance_trial(), as with any cancel.
  void end_autorun(Microseconds now_us);

  /// Where this board's settings are kept, or null on a board with nowhere to
  /// keep them. Null is the default and everything works without it: a board
  /// with no store is one whose settings do not survive a power cut, which is
  /// what every board did before there was a store at all.
  void set_settings_port(SettingsPort* p) { settings_ = p; }

  /// Read the stored settings and adopt them: the wiring, the graph set, and
  /// the autorun configuration -- which, if it says so, starts this board
  /// driving trials with no host in the picture at all.
  ///
  /// Called once at boot, before anything has greeted. Returns what the store
  /// said, and on anything but None nothing is adopted and the compiled-in
  /// defaults stand -- which is why those defaults have to be correct on their
  /// own. See STATEMACHINED_SAFE_LEVELS.
  SettingsError restore_settings(Microseconds now_us);

  /// How many times this board's store has been written. Reported so that flash
  /// wear is a number somebody can see: the RA4M1's data flash is good for
  /// about 100,000 erase cycles, and a rig that is a third of the way through
  /// that should be able to say so rather than failing one day.
  uint32_t settings_write_count() const { return settings_write_count_; }
  bool has_set() const { return have_set_; }
  uint16_t set_version() const { return live_set_.version; }
  uint8_t graph_count() const { return have_set_ ? live_set_.n_graphs : 0; }
  const GraphSet& graph_set() const { return live_set_; }
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
  void dispatch(const JsonObject& m, JsonSpan covered, uint16_t message_id,
                Microseconds now_us);

  // One handler per host command. Each is responsible for sending exactly one
  // reply, which is what makes a retry decidable for the bridge.
  void on_hello(const JsonObject& m, uint16_t message_id, Microseconds now_us);
  void on_upload_message(const JsonObject& m, JsonSpan covered, uint16_t message_id,
                         Microseconds now_us, MsgType type);
  void on_configure(const JsonObject& m, uint16_t message_id, Microseconds now_us);
  void on_start(const JsonObject& m, uint16_t message_id, Microseconds now_us);
  void on_cancel(const JsonObject& m, uint16_t message_id, Microseconds now_us);
  void on_ping(uint16_t message_id, Microseconds now_us);
  void on_wiring(const JsonObject& m, uint16_t message_id);
  void on_pins_request(const JsonObject& m, uint16_t message_id);
  void on_autorun(const JsonObject& m, uint16_t message_id, Microseconds now_us);
  void on_save(uint16_t message_id);

  /// Apply `configure`'s `patch`, remembering what to put back. False and a
  /// refusal already sent if any entry is unusable -- and nothing is applied in
  /// that case, so a malformed patch cannot leave a trial running with half of
  /// one.
  bool apply_distribution_patches(const JsonObject& m, uint16_t message_id);

  /// Put every patched distribution back. Called when a trial ends, when
  /// another `configure` replaces the patches, and whenever a session resets --
  /// a patch that outlived its trial would be a timing nobody could account
  /// for afterwards.
  void revert_distribution_patches();
  void on_state_request(uint16_t message_id, Microseconds now_us);

  /// A refusal of a command whose `message_id` was read: carries
  /// `in_reply_to`, and is remembered so that a resend of that command is
  /// answered rather than re-executed.
  void send_error(uint16_t message_id, const char* code, const char* message,
                  const char* context);

  /// A refusal of a line that never yielded a `message_id` -- too long, bad
  /// crc, unparsable, or simply missing the member. It carries no
  /// `in_reply_to` because there is genuinely nothing to name, and it is not
  /// remembered, because a line that did not identify itself cannot be
  /// recognised on a retry.
  ///
  /// Kept separate from send_error rather than signalled by passing 0: 0 is an
  /// ordinary message_id -- the counter wraps through it -- and a sentinel
  /// would silently give the session's first command the treatment meant for
  /// junk.
  void send_orphan_error(const char* code, const char* message, const char* context);
  void send_ack(uint16_t message_id);
  void emit_result();
  /// The unsolicited `started` for a trial the line began. Foreground only --
  /// the edge that sets `line_start_pending_` is found in the trial loop.
  void emit_line_started();

  /// Take a completed visit from whoever is advancing the trial. Cheap on
  /// purpose: a struct copy and two index bumps, because on a board this is the
  /// scan ISR. The formatting is drain_visits()' job.
  void record_visit(const StateVisit& v, uint32_t seq);

  /// Points the runner's visit sink back here. Called wherever `runner_` is
  /// rebuilt -- which is on every commit and every configure, because
  /// TrialRunner is replaced rather than reset -- so that a stream cannot go
  /// quiet because a graph was uploaded.
  void rebind_runner();

  /// Begin a run the device decided on for itself: its own trial id, the stored
  /// autorun seed, and the graph autorun was pointed at. Returns the entry
  /// state's output actions, which the scan applies.
  OutputUpdate start_autorun_trial(LineBitmask word, Microseconds now_us);

  /// Which state a trial that has been started but not yet scanned is in: the
  /// live graph's entry state, per-graph, or kNoState if there is no graph.
  /// What state_report answers while `start_pending_` is up.
  StateIndex entry_state_of_live_graph() const;

  /// Complete a `start` the scan has not reached yet, and return what its entry
  /// state owes the pins. A no-op, returning nothing, if none is pending.
  ///
  /// The ordinary path is advance_trial(), which is the whole point: a trial
  /// begins on the scan that drives its entry action's pins. But a cancel or a
  /// link loss can arrive inside that window -- under one scan period, and only
  /// from a host that did not wait for `started` -- and a cancel has to have a
  /// trial to cancel, or the run ends with no account of it. So it is started
  /// here, on the spot, and ended by the caller exactly as any other running
  /// trial is.
  OutputUpdate start_now_if_pending(Microseconds now_us);

  /// The machine reports a visit; the session says whose trial it was. A
  /// separate object rather than making the session a VisitSink, so that
  /// `on_visit` is not part of what a caller can reach.
  class VisitRelay : public VisitSink {
   public:
    explicit VisitRelay(HostLinkSession* s) : session_(s) {}
    void on_visit(const StateVisit& v, uint32_t seq) override {
      session_->record_visit(v, seq);
    }

   private:
    HostLinkSession* session_;
  };

  /// The machine applies a TimerStart or TimerCancel action; the bank does the
  /// work. Separate from the session for the same reason VisitRelay is: the
  /// callback is the machine's business and not part of the session's surface.
  ///
  /// The outputs it produces are accumulated rather than returned, because the
  /// machine has no way to give them back -- it is midway through building its
  /// own OutputUpdate. advance_trial() merges them in on the same scan, so a
  /// timer's line and the entry action beside it move together.
  class TimerRelay : public TimerActionSink {
   public:
    explicit TimerRelay(HostLinkSession* s) : session_(s) {}
    void on_timer_action(uint8_t timer, bool start, Microseconds now_us) override {
      session_->run_timer_action(timer, start, now_us);
    }

   private:
    HostLinkSession* session_;
  };

  void run_timer_action(uint8_t timer, bool start, Microseconds now_us);

  /// Put the device's own enable mask back after a trial that overrode it.
  /// A no-op when `configure` did not, which is the common case.
  void revert_timer_patch(Microseconds now_us);

  /// `timers`: which global timers may run. See GlobalTimerBank::set_enabled.
  void on_timers(const JsonObject& m, uint16_t message_id, Microseconds now_us);

  /// Finish, frame and send whatever is in the transmit buffer, remembering it
  /// as the answer to `message_id` so a retry can be answered from the cache.
  void send(JsonWriter& w, uint16_t message_id);
  void send_unsolicited(JsonWriter& w);

  ReplySink& out_;
  DeviceIdentity identity_;

  char rx_[kMaxLine];
  LineReader reader_;
  char tx_[kMaxLine];

  /// One set, built in place by the builder. Two do not fit on a 32 KB board:
  /// see the note at the top of graph_builder.h for what that costs.
  GraphSet live_set_;
  GraphBuilder builder_{live_set_};
  bool have_set_ = false;

  PatchedDistribution patched_distributions_[kMaxPatchedDistributions];
  uint8_t n_patched_distributions_ = 0;

  DeviceWiring wiring_;
  bool have_wiring_ = false;
  uint16_t wiring_revision_ = 0;

  TrialRunner runner_;
  VisitRelay visit_relay_{this};
  TimerRelay timer_relay_{this};

  /// The global timers. Here rather than in StateMachine because they outlive
  /// the run: the session builds a new TrialRunner per trial, which would reset
  /// them at every boundary. See machine/global_timers.h.
  GlobalTimerBank timers_;
  /// Outputs a TimerStart or TimerCancel action produced while the machine was
  /// midway through its own update, waiting to be merged into the same scan's.
  OutputUpdate timer_action_ops_;

  /// The enable mask the device holds between trials, and the one this trial
  /// was configured with.
  ///
  /// Two, so that `configure`'s override lasts exactly one trial and the device
  /// goes back to what it was told at rig level -- the same discipline as a
  /// distribution `patch`, and for the same reason: a mask that outlived its
  /// trial would be a timer running, or not running, that nobody could account
  /// for afterwards.
  uint32_t timers_enabled_ = 0xFFFFFFFFu;
  bool timers_patched_ = false;
  LinkState state_ = LinkState::Greeting;

  uint64_t session_seed_ = 0;
  uint32_t armed_trial_id_ = 0;
  bool start_from_serial_ = true;

  /// The armed trial starts on the rising edge of `start_line_`, and has not
  /// seen it yet. Cleared the moment that edge starts the run, so one edge
  /// starts one trial and a line left high does not start the next.
  bool start_from_line_ = false;
  uint8_t start_line_ = 0;

  /// A run the *line* began and the host has not been told about.
  ///
  /// The host did not ask for this one, so nothing is waiting on a reply and
  /// the only way it learns the trial is in flight is an unsolicited `started`.
  /// Flagged rather than sent for the reason every other line on this device is
  /// flagged rather than sent: the edge is detected in the trial loop, which on
  /// a board is the timer ISR, and send_line() spins there forever. Sent by
  /// drain_outbound(), from the foreground.
  bool line_start_pending_ = false;

  /// The scan that saw the edge -- which for a line start is the real start of
  /// the trial, not an acknowledgement of a command. Held because the
  /// foreground that sends it runs a pass or so later.
  Microseconds line_started_at_us_ = 0;

  /// Has anybody said hello? The gate on every other command, and emphatically
  /// not the same question as `state_`: a board driving itself from storage is
  /// Running with nobody having greeted it, and it must still refuse a
  /// `configure` from a host that skipped the handshake.
  bool greeted_ = false;

  AutorunConfig autorun_;
  bool autorun_active_ = false;
  /// When the next self-driven run is due, meaningful in LinkState::Relighting.
  /// Compared with the signed difference every deadline on this device uses, so
  /// it survives the microsecond counter wrapping.
  Microseconds relight_at_us_ = 0;
  /// The id the next self-driven run gets. Counts on from the last one, so a
  /// board left running overnight does not report a thousand trials all called
  /// 1.
  uint32_t autorun_next_trial_id_ = 1;

  SettingsPort* settings_ = nullptr;
  uint32_t settings_write_count_ = 0;

  DuplicateCommandGuard guard_;
  uint16_t tx_message_id_ = 0;
  Microseconds booted_us_ = 0;
  bool have_boot_ = false;
  uint32_t bad_lines_ = 0;
  ScanHealth scan_;
  LineBitmask last_word_ = 0;

  /// Outputs owed to the pins by something that happened between scans -- a
  /// cancel, a fail-safe, a start that beat the scan. The link's thread of
  /// control drives nothing; advance_trial() does. Without this they would be
  /// returned to nobody.
  OutputUpdate pending_ops_;

  /// Visits recorded but not yet sent.
  ///
  /// Deep enough to cover the longest the foreground can go without draining,
  /// which is one link command -- about 2.5 ms, or 25 scans at 10 kHz, against
  /// the worst gap of 22 measured on a board. Not sized to hold a trial: a
  /// graph with a loop produces far more visits than it has states, and the
  /// record that must be complete is `result_path` (kMaxPath), which is built
  /// from the machine's own record and never from this.
  static constexpr uint8_t kVisitRingDepth = 32;

  struct PendingVisit {
    StateVisit visit;
    uint32_t seq = 0;
    /// Captured at record time, not at send time: by the time this is
    /// serialised the trial may have ended and `state_` moved on, and a visit
    /// that reported trial 0 because it was sent late would join to nothing.
    uint32_t trial_id = 0;
  };

  PendingVisit visit_ring_[kVisitRingDepth];
  /// One producer (whoever advances the trial) writes only `head`, one consumer
  /// (drain_visits) writes only `tail`. That is what makes this safe with no
  /// critical section, on a device where the producer is an interrupt -- and it
  /// is why a full ring drops the *newest* visit rather than the oldest, which
  /// would need the producer to move `tail` too. Either way the host sees a
  /// counted gap in `seq`; only this one needs no lock.
  volatile uint8_t visit_head_ = 0;
  volatile uint8_t visit_tail_ = 0;
  uint32_t dropped_visits_ = 0;

  /// A run that has ended and whose result has not been sent. Set by the trial
  /// loop, cleared by drain_outbound(). One pass of the foreground long, and
  /// the autorun relight waits for it so that the next run cannot reset the
  /// record the result is built from.
  bool result_pending_ = false;

  /// A `start` the host has been told about and the scan has not run yet.
  ///
  /// The gap is one scan period, and it exists on purpose: the trial begins on
  /// the scan, so that its `entered_us` and the pins its entry action drives
  /// are the same instant rather than a millisecond apart. See on_start() for
  /// the measurement that made this necessary, and start_now_if_pending() for
  /// the one path that closes the gap early.
  ///
  /// `state_` is Running throughout -- the host was told the trial started, and
  /// `runner_.running()` is the question about the engine, not about the wire.
  bool start_pending_ = false;
};

}  // namespace statemachined
