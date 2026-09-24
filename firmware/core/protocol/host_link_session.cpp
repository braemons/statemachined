// SPDX-License-Identifier: GPL-3.0-or-later
#include "protocol/host_link_session.h"

#include <pb_decode.h>
#include <pb_encode.h>

#include "protocol/crc16.h"

namespace statemachined {

// The limits in core/proto/link.options, held to the ones this build was
// configured with. A board whose link could carry fewer options than its pool
// holds would refuse a set it has room for; one whose frame could not hold the
// largest message would fail to send it.
static_assert(sizeof(statemachined_link_v1_GraphDist{}.opts) / sizeof(int32_t) >=
                  kMaxChoiceOptions,
              "link.options: GraphDist.opts holds kMaxChoiceOptions");
static_assert(sizeof(statemachined_link_v1_Configure{}.patch) /
                      sizeof(statemachined_link_v1_Patch) ==
                  kMaxPatchedDistributions,
              "link.options: Configure.patch holds kMaxPatchedDistributions");
static_assert(sizeof(statemachined_link_v1_Wiring{}.debounce_ms) / sizeof(uint32_t) ==
                  kMaxLines,
              "link.options: Wiring.debounce_ms holds kMaxLines");
static_assert(sizeof(statemachined_link_v1_PinMap{}.pins) /
                      sizeof(statemachined_link_v1_PinMap{}.pins[0]) >=
                  kMaxLines,
              "link.options: PinMap.pins holds kMaxLines");
static_assert(statemachined_link_v1_HostMessage_size <= kMaxPayload,
              "the largest host message fits a frame");
static_assert(statemachined_link_v1_DeviceMessage_size <= kMaxPayload,
              "the largest device message fits a frame");

namespace {

/// 2: the protobuf link. 1 was the NDJSON wire, which no board speaks now.
constexpr uint16_t kProtocolVersion = 2;

/// How many rows one result_path carries: what link.options gives `p`.
constexpr uint8_t kResultRowsPerFrame =
    sizeof(statemachined_link_v1_ResultPath{}.p) / sizeof(statemachined_link_v1_StateVisit);

/// Empty messages to reset `rx_` and `tx_` from. nanopb's `_init_zero` is a
/// brace list, which older compilers take as an initialiser and not as the
/// right-hand side of an assignment.
const link::HostMessage kNoHostMessage = statemachined_link_v1_HostMessage_init_zero;
const link::DeviceMessage kNoDeviceMessage = statemachined_link_v1_DeviceMessage_init_zero;

/// A wire value that has to fit a byte, refused rather than truncated.
bool narrow_u8(uint32_t value, uint8_t* out) {
  if (value > UINT8_MAX) return false;
  *out = static_cast<uint8_t>(value);
  return true;
}

/// Copy a constant string into one of the link's fixed-size fields. Truncated
/// rather than overflowed; every string this device sends is its own constant,
/// and link.options sizes the fields for them.
template <size_t N>
void put_text(char (&field)[N], const char* text) {
  size_t i = 0;
  if (text != nullptr)
    for (; i + 1 < N && text[i] != '\0'; ++i) field[i] = text[i];
  field[i] = '\0';
}

/// Would `text` fit `field` whole? For the one string a board may hold that is
/// not a constant of this file: a pin label.
template <size_t N>
bool fits(const char (&)[N], const char* text) {
  size_t length = 0;
  while (text[length] != '\0') ++length;
  return length < N;
}

/// Add `later` to the outputs already owed, with `later` winning where the two
/// touch the same line. Written out by hand in four places before this existed,
/// and getting the `& ~` half of it wrong is a line that silently stays put.
void owe(OutputUpdate& into, const OutputUpdate& later) {
  into.set_high = (into.set_high | later.set_high) & ~later.set_low;
  into.set_low = (into.set_low | later.set_low) & ~later.set_high;
}

/// Microseconds between two device-clock readings, correct across the ~71
/// minute wrap because unsigned subtraction is.
Microseconds since(Microseconds from, Microseconds to) { return to - from; }

/// Has `deadline` passed? The signed difference, so it stays right across that
/// same wrap -- correct while the deadline is within ~35 minutes of now, which
/// an inter-trial interval is.
bool reached(Microseconds deadline, Microseconds now) {
  return static_cast<int32_t>(now - deadline) >= 0;
}

statemachined_link_v1_ExitCause exit_cause(StateExitCause c) {
  switch (c) {
    case StateExitCause::Timeout:
      return statemachined_link_v1_ExitCause_EXIT_CAUSE_TIMEOUT;
    case StateExitCause::Transition:
      return statemachined_link_v1_ExitCause_EXIT_CAUSE_TRANSITION;
    case StateExitCause::Cancel:
      return statemachined_link_v1_ExitCause_EXIT_CAUSE_CANCEL;
    case StateExitCause::Terminal:
      return statemachined_link_v1_ExitCause_EXIT_CAUSE_TERMINAL;
  }
  return statemachined_link_v1_ExitCause_EXIT_CAUSE_TERMINAL;
}

/// One visit, as a result_path row and a visit's `v` both carry it -- one
/// shape for one fact, filled in one place.
link::StateVisit visit_row(const StateVisit& v) {
  link::StateVisit row = statemachined_link_v1_StateVisit_init_zero;
  row.state = v.state_index;
  row.exit = exit_cause(v.cause);
  row.transition = v.transition_index;
  row.drawn_ms = v.drawn_ms;
  row.entered_us = v.entered_us;
  row.duration_us = v.duration_us;
  return row;
}

/// The protocol error code for an upload failure. docs/reference/protocol.md 5 -- the
/// bridge switches on this, so the mapping is here rather than improvised at
/// each call site.
const char* upload_error_code(UploadError e) {
  switch (e) {
    case UploadError::None:
      return "internal";
    case UploadError::NotOpen:
      return "not_ready";
    case UploadError::BadOrder:
      return "bad_order";
    case UploadError::BadIndex:
      return "bad_index";
    case UploadError::TooMany:
      return "too_many";
    case UploadError::BadField:
      return "bad_field";
    case UploadError::CountMismatch:
    case UploadError::ChecksumMismatch:
    case UploadError::Invalid:
      return "bad_graph";
  }
  return "internal";
}

}  // namespace

void DuplicateCommandGuard::remember(uint16_t message_id, const uint8_t* frame, size_t n) {
  if (n > sizeof(reply_)) {  // cannot happen: every frame is built in a kMaxFrame buffer
    have_ = false;
    return;
  }
  for (size_t i = 0; i < n; ++i) reply_[i] = frame[i];
  reply_len_ = n;
  last_message_id_ = message_id;
  have_ = true;
}

HostLinkSession::HostLinkSession(ReplySink& out, const DeviceIdentity& identity)
    : out_(out), identity_(identity), runner_(live_set_) {
  rebind_runner();
}

void HostLinkSession::rebind_runner() {
  runner_.set_visit_sink(&visit_relay_);
  runner_.set_timer_sink(&timer_relay_);
}

void HostLinkSession::revert_timer_patch(Microseconds now_us) {
  if (!timers_patched_) return;
  timers_patched_ = false;
  const OutputUpdate ops = timers_.set_enabled(timers_enabled_, now_us);
  // Owed rather than returned: this is called from the end of a run and from
  // paths that have no update of their own to hand back.
  owe(pending_ops_, ops);
}

void HostLinkSession::run_timer_action(uint8_t timer, bool start, Microseconds now_us) {
  // Accumulated, not returned: the machine is midway through building its own
  // update and has nowhere to put these. advance_trial() folds them into the
  // same scan's output, so a timer's line and the entry action that started it
  // reach the pins together.
  const OutputUpdate ops = start ? timers_.start(timer, now_us) : timers_.cancel(timer, now_us);
  owe(timer_action_ops_, ops);
}

// ------------------------------------------------------------- receiving ---

void HostLinkSession::receive(const uint8_t* bytes, size_t n, Microseconds now_us) {
  for (size_t i = 0; i < n; ++i) receive_byte(bytes[i], now_us);
}

void HostLinkSession::receive_byte(uint8_t c, Microseconds now_us) {
  if (!reader_.feed(c)) return;

  if (reader_.status() != FrameError::None) {
    // Checked before decoded, always. The frame never arrived intact, so there
    // is no message_id to attribute the error to -- and nothing in it can be
    // believed, the message_id included. Reported anyway rather than dropped:
    // a host waiting for an answer it will never get is worse than one told
    // its frame was unusable.
    send_orphan_error(frame_error_code(reader_.status()), frame_error_str(reader_.status()),
                      "frame");
    return;
  }
  handle_frame(link::PayloadSpan{reader_.payload(), reader_.len()}, now_us);
}

void HostLinkSession::handle_frame(link::PayloadSpan payload, Microseconds now_us) {
  rx_ = kNoHostMessage;
  pb_istream_t stream = pb_istream_from_buffer(payload.bytes, payload.len);
  if (!pb_decode(&stream, statemachined_link_v1_HostMessage_fields, &rx_)) {
    // Intact and still not a message: a list longer than this board holds, a
    // message_id wider than sixteen bits, or bytes that are not protobuf.
    ++bad_lines_;
    send_orphan_error("bad_message", PB_GET_ERROR(&stream), "frame");
    return;
  }

  // A retry of the command just answered gets that answer back verbatim and
  // changes nothing. Acting twice is what matters: a re-executed start would
  // run a second trial.
  if (guard_.is_repeat(rx_.message_id)) {
    out_.send_frame(guard_.reply(), guard_.reply_len());
    return;
  }

  dispatch(rx_, payload, now_us);
}

void HostLinkSession::dispatch(const link::HostMessage& m, link::PayloadSpan payload,
                               Microseconds now_us) {
  const uint16_t message_id = m.message_id;

  // hello is the only thing accepted before a hello: everything else needs a
  // session seed, and a device answering commands without one would be running
  // trials nobody could replay.
  // Not `state_ == Greeting`: a board driving itself from stored settings is
  // Running with nobody having greeted it, and it must still refuse every
  // command from a host that skipped the handshake.
  if (!greeted_ && m.which_body != statemachined_link_v1_HostMessage_hello_tag) {
    send_error(message_id, "not_ready", "no hello yet", "hello");
    return;
  }

  switch (m.which_body) {
    case statemachined_link_v1_HostMessage_hello_tag:
      return on_hello(m.body.hello, message_id, now_us);
    case statemachined_link_v1_HostMessage_ping_tag:
      return on_ping(message_id, now_us);
    case statemachined_link_v1_HostMessage_state_tag:
      return on_state_request(message_id, now_us);
    case statemachined_link_v1_HostMessage_wiring_tag:
      return on_wiring(m.body.wiring, message_id);
    case statemachined_link_v1_HostMessage_timers_tag:
      return on_timers(m.body.timers, message_id, now_us);
    case statemachined_link_v1_HostMessage_pins_tag:
      return on_pins_request(m.body.pins, message_id);
    case statemachined_link_v1_HostMessage_autorun_tag:
      return on_autorun(m.body.autorun, message_id, now_us);
    case statemachined_link_v1_HostMessage_save_tag:
      return on_save(message_id);
    case statemachined_link_v1_HostMessage_configure_tag:
      return on_configure(m.body.configure, message_id, now_us);
    case statemachined_link_v1_HostMessage_start_tag:
      return on_start(m.body.start, message_id, now_us);
    case statemachined_link_v1_HostMessage_cancel_tag:
      return on_cancel(m.body.cancel, message_id, now_us);

    case statemachined_link_v1_HostMessage_set_begin_tag:
    case statemachined_link_v1_HostMessage_set_end_tag:
    case statemachined_link_v1_HostMessage_graph_begin_tag:
    case statemachined_link_v1_HostMessage_graph_dist_tag:
    case statemachined_link_v1_HostMessage_graph_state_tag:
    case statemachined_link_v1_HostMessage_graph_transition_tag:
    case statemachined_link_v1_HostMessage_graph_action_tag:
    case statemachined_link_v1_HostMessage_graph_timer_tag:
    case statemachined_link_v1_HostMessage_graph_end_tag:
      return on_upload_message(m, payload, now_us);

    default:
      break;
  }

  // A body this firmware does not know -- a newer daemon's command, which
  // nanopb skipped as an unknown field and left `which_body` at zero. Refused
  // by name rather than dropped: a host waiting on a reply to it is told.
  send_error(message_id, "unknown_type", "unrecognised message type", "body");
}

// -------------------------------------------------------------- handlers ---

void HostLinkSession::on_hello(const link::Hello& m, uint16_t message_id, Microseconds now_us) {
  if (m.proto != kProtocolVersion) {
    send_error(message_id, "bad_proto", "this firmware does not speak that version", "proto");
    return;
  }
  const uint64_t seed = m.seed;

  // Resets to idle and abandons any half-finished upload, but deliberately does
  // NOT clear the committed graph: reconnecting the bridge must not cost a
  // re-upload.
  session_seed_ = seed;
  // The timers get their own stream from the same seed, so a session replays
  // whole. Not the trial stream: see GlobalTimerBank::reseed.
  timers_.reseed(seed);
  // A host that greets takes the rig. A board driving itself has to stop --
  // through the ordinary exit path, so whatever the current state raised comes
  // down -- because two authorities arming trials on one box is a rig running
  // trials nobody ordered. The stored setting survives, so the next boot still
  // comes up self-driving; what ends is this board doing it while somebody is
  // watching. It has to be an explicit `autorun` to start again, and that is
  // the point: a daemon that crashed must not be able to leave an animal being
  // rewarded by a box nobody is attending.
  autorun_active_ = false;
  greeted_ = true;
  revert_distribution_patches();
  builder_.abandon();
  reader_.reset();
  // A hello ends whatever was running, and ends it through the ordinary exit
  // path: the run is cancelled here and the next scan emits its result and
  // lowers its lines, exactly as a `cancel` from the host does. Resetting
  // straight to Idle instead -- which is what this did while only a host could
  // start a trial -- abandoned the run silently, leaving no result and its
  // lines up until something else happened to move them.
  if (state_ == LinkState::Running) {
    runner_.cancel(TrialCancelReason::Host, now_us);
  } else {
    state_ = LinkState::Idle;
  }
  armed_trial_id_ = 0;
  start_from_line_ = false;

  auto& ack = compose(statemachined_link_v1_DeviceMessage_hello_ack_tag).body.hello_ack;
  ack.proto = kProtocolVersion;
  put_text(ack.board, identity_.board);
  put_text(ack.fw, identity_.firmware_version);
  ack.n_input_lines = identity_.input_line_count;
  ack.n_output_lines = identity_.output_line_count;
  ack.scan_hz = identity_.measured_scan_hz;
  ack.has_caps = true;
  ack.caps.max_frame = kMaxFrame;
  ack.caps.max_states = kMaxStates;
  ack.caps.max_transitions = kMaxTransitions;
  ack.caps.max_output_actions = kMaxOutputActions;
  ack.caps.max_distributions = kMaxDistributions;
  ack.caps.max_choice_options = kMaxChoiceOptions;
  ack.caps.max_path = kMaxPath;
  ack.caps.max_graphs = kMaxGraphs;
  ack.caps.max_timers = kMaxTimers;
  // Which input line the first timer holds high. Not derivable from the line
  // count -- the timers are counted down from the top of the word so that a
  // graph means the same thing on a board with eight inputs and one with
  // twenty -- so a host that wants to write a predicate against a timer has to
  // be told. See config.h.
  ack.caps.first_timer_line = kFirstTimerLine;
  ack.has_set = have_set_;
  ack.set_version = have_set_ ? live_set_.version : 0;
  ack.n_graphs = have_set_ ? live_set_.n_graphs : 0;
  // Whether anybody has told this board what it is wired to. False means it is
  // running the compile-time defaults, which a daemon needs to know before it
  // decides whether to push a wiring or to trust the one that is there.
  ack.has_wiring = have_wiring_;
  send(message_id);
}

void HostLinkSession::on_timers(const link::Timers& m, uint16_t message_id,
                                Microseconds now_us) {
  // Refused mid-trial for the reason `wiring` is: this changes what the scan
  // does, and a timer switched off under a running trial would move a timing
  // that trial's record could not account for. A trial that wants a different
  // mask says so in its own `configure`, which is the whole point of the
  // per-trial override.
  if (state_ == LinkState::Armed || state_ == LinkState::Running) {
    send_error(message_id, "busy", "a trial is armed or running", "timers");
    return;
  }
  const uint32_t enable = m.enable;
  timers_enabled_ = enable;
  // Applied now, not at the next trial: between trials is exactly when a
  // free-running timer is doing something, and "off" has to mean off.
  owe(pending_ops_, timers_.set_enabled(enable, now_us));

  auto& ack = compose(statemachined_link_v1_DeviceMessage_ack_tag).body.ack;
  // Echoed, so a host never has to infer what took: a mask naming timers the
  // set does not declare is accepted and simply has no effect, and this says
  // what the device is actually holding.
  ack.has_enable = true;
  ack.enable = enable;
  ack.has_n_timers = true;
  ack.n_timers = live_set_.n_timers;
  send(message_id);
}

void HostLinkSession::on_pins_request(const link::Pins& m, uint16_t message_id) {
  // docs/reference/protocol.md 3.7. One direction per request, and `dir` is
  // required.
  //
  // Not both in one reply: a reply that silently held half of the labels would
  // be worse than no reply at all -- the host would believe it had the whole
  // map. One direction always fits, so this needs no chunking and no partial
  // answer.
  const bool inputs = m.dir == statemachined_link_v1_Direction_DIRECTION_IN;
  if (!inputs && m.dir != statemachined_link_v1_Direction_DIRECTION_OUT) {
    send_error(message_id, "bad_field", "dir is \"in\" or \"out\"", "dir");
    return;
  }

  const char* const* labels = inputs ? identity_.input_pin_labels : identity_.output_pin_labels;
  const uint8_t count = inputs ? identity_.input_line_count : identity_.output_line_count;
  if (labels == nullptr) {
    // Nothing invented. A host that gets this keeps whatever it assumed and --
    // this is the point -- knows that it assumed it.
    send_error(message_id, "no_pin_map", "this build does not name its pins", "dir");
    return;
  }

  auto& map = compose(statemachined_link_v1_DeviceMessage_pin_map_tag).body.pin_map;
  map.dir = inputs ? statemachined_link_v1_Direction_DIRECTION_IN
                   : statemachined_link_v1_Direction_DIRECTION_OUT;
  map.n = count;
  map.pins_count = count;
  for (uint8_t i = 0; i < count; ++i) {
    const char* label = labels[i] != nullptr ? labels[i] : "";
    if (!fits(map.pins[i], label)) {
      // A label longer than link.options allows. Refused rather than
      // truncated: "D1" where the board says "D10" is a wire in the wrong hole.
      send_error(message_id, "too_long", "a pin label does not fit the link", "pins");
      return;
    }
    put_text(map.pins[i], label);
  }
  send(message_id);
}

void HostLinkSession::set_wiring(const DeviceWiring& w, bool from_host) {
  wiring_ = w;
  if (from_host) have_wiring_ = true;
  ++wiring_revision_;
}

void HostLinkSession::on_wiring(const link::Wiring& m, uint16_t message_id) {
  // Refused mid-trial for the reason a graph upload is: the conditioning it
  // changes is read by the scan, and changing a debounce under a running trial
  // would move a timing nobody could account for afterwards.
  if (state_ == LinkState::Armed || state_ == LinkState::Running) {
    send_error(message_id, "busy", "a trial is armed or running", "wiring");
    return;
  }

  // Read into a copy and install it whole, so a message that turns out to be
  // malformed halfway through leaves the board wired the way it was.
  DeviceWiring next = wiring_;
  if (m.has_invert) next.inputs.invert_mask = m.invert;
  if (m.has_enable) next.inputs.enable_mask = m.enable;
  if (m.has_safe) next.output_safe_levels = m.safe;
  // Empty leaves the table alone, since protobuf cannot tell an empty list from
  // an absent one. Anything else replaces every line, not only the ones it
  // names: a shorter list means the rest are zero, so that removing a debounce
  // is possible at all -- and `[0]` removes them all.
  if (m.debounce_ms_count != 0) {
    for (uint8_t i = 0; i < kMaxLines; ++i) next.inputs.debounce_ms[i] = 0;
    for (pb_size_t i = 0; i < m.debounce_ms_count; ++i) {
      if (m.debounce_ms[i] > UINT16_MAX) {
        send_error(message_id, "bad_field", "a debounce is out of range", "debounce_ms");
        return;
      }
      next.inputs.debounce_ms[i] = static_cast<NarrowMilliseconds>(m.debounce_ms[i]);
    }
  }

  const LineBitmask previous_safe_levels = wiring_.output_safe_levels;
  set_wiring(next, /*from_host=*/true);

  // Adopt new safe levels straight away. A `wiring` is only accepted while no
  // trial is armed or running (above), so there is nothing this can disturb --
  // and the alternative is a board that has been told what "safe" means here
  // and is still driving the previous rig's idea of it until the next reset.
  //
  // Through pending_ops_ rather than by driving anything: this is the link's
  // thread of control and it touches no pin. The next scan applies it, exactly
  // as it does for the outputs a start() owes.
  if (next.output_safe_levels != previous_safe_levels) {
    const OutputUpdate safe = fail_safe();
    pending_ops_.set_high = (pending_ops_.set_high | safe.set_high) & ~safe.set_low;
    pending_ops_.set_low = (pending_ops_.set_low | safe.set_low) & ~safe.set_high;
  }

  send_ack(message_id);
}

void HostLinkSession::on_upload_message(const link::HostMessage& m, link::PayloadSpan payload,
                                        Microseconds now_us) {
  const uint16_t message_id = m.message_id;
  if (state_ == LinkState::Armed || state_ == LinkState::Running) {
    send_error(message_id, "busy", "a trial is armed or running", "graph upload");
    return;
  }

  UploadError e = UploadError::None;
  bool is_end = false;
  switch (m.which_body) {
    case statemachined_link_v1_HostMessage_set_begin_tag:
      // From here until set_end succeeds the board holds no graph. The builder
      // writes into the live set because two do not fit, so this is where the
      // old double buffering went -- see graph_builder.h.
      have_set_ = false;
      e = builder_.begin_set(m.body.set_begin, payload);
      break;
    case statemachined_link_v1_HostMessage_graph_begin_tag:
      e = builder_.begin_graph(m.body.graph_begin, payload);
      break;
    case statemachined_link_v1_HostMessage_graph_dist_tag:
      e = builder_.add_distribution(m.body.graph_dist, payload);
      break;
    case statemachined_link_v1_HostMessage_graph_state_tag:
      e = builder_.add_state(m.body.graph_state, payload);
      break;
    case statemachined_link_v1_HostMessage_graph_transition_tag:
      e = builder_.add_transition(m.body.graph_transition, payload);
      break;
    case statemachined_link_v1_HostMessage_graph_action_tag:
      e = builder_.add_action(m.body.graph_action, payload);
      break;
    case statemachined_link_v1_HostMessage_graph_timer_tag:
      e = builder_.add_timer(m.body.graph_timer, payload);
      break;
    case statemachined_link_v1_HostMessage_graph_end_tag:
      e = builder_.end_graph(m.body.graph_end, payload);
      break;
    default:  // set_end; dispatch admits no other body here
      e = builder_.end_set(m.body.set_end);
      is_end = true;
      break;
  }

  if (e != UploadError::None) {
    send_error(message_id, upload_error_code(e), upload_error_str(e), builder_.context());
    return;
  }

  if (!is_end) {
    send_ack(message_id);
    return;
  }

  have_set_ = true;
  // Graph 0 until a configure says otherwise, so a board that has just been
  // given a set is in a defined state rather than pointing at a slot nobody
  // chose.
  runner_ = TrialRunner(live_set_, 0);
  rebind_runner();
  // The new set's timers, and none of the old set's. bind() puts back any line
  // a timer of the previous set was holding up -- nothing else would, and an
  // upload is refused while a trial is armed or running, so this is the only
  // moment it can be done.
  owe(pending_ops_, timers_.bind(&live_set_, now_us));

  auto& ok = compose(statemachined_link_v1_DeviceMessage_set_ok_tag).body.set_ok;
  ok.set_version = live_set_.version;
  ok.n_graphs = live_set_.n_graphs;
  ok.n_states = live_set_.n_states;
  ok.n_transitions = live_set_.n_transitions;
  ok.n_output_actions = live_set_.n_output_actions;
  send(message_id);
}

// ---------------------------------------------------------------- autorun ---

bool HostLinkSession::begin_autorun(const AutorunConfig& c, Microseconds now_us) {
  // A board that armed itself against a set it does not have would be a rig
  // running nothing while reporting that it is running something.
  if (!have_set_ || c.graph_index >= live_set_.n_graphs) return false;

  end_autorun(now_us);
  autorun_ = c;
  autorun_.enabled = true;
  autorun_active_ = true;
  autorun_next_trial_id_ = c.first_trial_id;

  runner_ = TrialRunner(live_set_, autorun_.graph_index);
  rebind_runner();
  runner_.set_trial_cap_ms(autorun_.cap_ms);

  // Due immediately, and started by the next scan rather than here: this is not
  // the scan's thread of control, and nothing else in this class drives a pin
  // from anywhere else either.
  state_ = LinkState::Relighting;
  relight_at_us_ = now_us;
  return true;
}

void HostLinkSession::end_autorun(Microseconds now_us) {
  if (!autorun_active_) return;
  autorun_active_ = false;
  if (state_ == LinkState::Running) {
    // Through the ordinary exit path, like every other cancel: whatever the
    // current state raised comes down by the code that always lowers it, the
    // outputs are owed to the next advance_trial(), and that same scan emits
    // the result. Forcing Idle here instead would end the run with no account
    // of it, which is the one thing this device is for.
    runner_.cancel(TrialCancelReason::Host, now_us);
    return;
  }
  state_ = LinkState::Idle;
}

void HostLinkSession::on_autorun(const link::Autorun& m, uint16_t message_id,
                                 Microseconds now_us) {
  // No `enabled` is a question rather than an instruction: report what the
  // board would do, change nothing. Worth having as its own shape because the
  // settings outlive the session that set them, so "what are you configured to
  // do on your own?" is a question a freshly connected daemon has.
  const bool asking = !m.has_enabled;

  if (!asking) {
    const bool enabled = m.enabled;
    AutorunConfig c = autorun_;
    if (m.has_graph_index && !narrow_u8(m.graph_index, &c.graph_index)) {
      send_error(message_id, "bad_field", "graph_index", "graph_index");
      return;
    }
    if (m.has_cap_ms) c.cap_ms = m.cap_ms;
    if (m.has_seed) c.seed = m.seed;
    if (m.has_first_trial_id) c.first_trial_id = m.first_trial_id;

    // Whether to start driving *now*, as opposed to merely recording that this
    // board should. The two are separate because saving requires an idle board:
    // a board already arming its own trials is never idle, so "enable it, then
    // write it down" would be a sequence that could not be performed. With
    // this, a rig is set up while nothing is running -- enable, save, power
    // cycle -- and comes up self-driving from its own storage.
    const bool start_now = m.has_start_now ? m.start_now : true;

    if (enabled) {
      if (!have_set_) {
        send_error(message_id, "not_ready", "no graph set has been committed", "graph");
        return;
      }
      if (c.graph_index >= live_set_.n_graphs) {
        send_error(message_id, "bad_index", "no graph in that slot", "graph_index");
        return;
      }
      // Refused rather than silently taking effect at the end of the trial: a
      // command that hands the board a *new* job must not land while it is
      // doing one. Turning autorun OFF is deliberately not refused -- "stop" is
      // the thing somebody most wants while it is running, and it ends the run
      // through the ordinary exit path exactly as `cancel` does.
      if (state_ == LinkState::Running && !autorun_active_) {
        send_error(message_id, "busy", "a trial is running", "trial");
        return;
      }
      if (start_now) {
        begin_autorun(c, now_us);
      } else {
        end_autorun(now_us);
        autorun_ = c;
        autorun_.enabled = true;
        autorun_next_trial_id_ = c.first_trial_id;
      }
    } else {
      end_autorun(now_us);
      autorun_ = c;
      autorun_.enabled = false;
    }
  }

  auto& ok = compose(statemachined_link_v1_DeviceMessage_autorun_ok_tag).body.autorun_ok;
  ok.enabled = autorun_.enabled;
  // Not the same fact: the setting is stored and survives a takeover, while
  // `active` is whether this board is driving trials right now. A daemon that
  // greeted a self-driving board sees enabled true and active false, which is
  // exactly what happened.
  ok.active = autorun_active_;
  ok.graph_index = autorun_.graph_index;
  ok.cap_ms = autorun_.cap_ms;
  ok.next_trial_id = autorun_next_trial_id_;
  send(message_id);
}

// --------------------------------------------------------------- settings ---

SettingsError HostLinkSession::restore_settings(Microseconds now_us) {
  if (settings_ == nullptr || !settings_->has_storage()) return SettingsError::Io;

  StoredSettings stored;
  stored.set = &live_set_;
  const SettingsError e = settings_->load(stored);
  if (e != SettingsError::None) return e;

  settings_write_count_ = stored.write_count;
  // `from_host` is false: hello_ack's has_wiring says whether a *host* has
  // configured this board, and a wiring this board remembered about itself is
  // not that. The daemon still needs to know it should push one.
  set_wiring(stored.wiring, false);

  if (stored.has_set) {
    // Validated, not trusted. The record's CRC says the bytes are the ones that
    // were written; it says nothing about whether they were a graph worth
    // running, and a set that came back with an index out of range would fault
    // the first time a trial reached it.
    if (validate(live_set_) != GraphError::None) {
      live_set_ = GraphSet{};
      have_set_ = false;
      return SettingsError::TooBig;
    }
    have_set_ = true;
    runner_ = TrialRunner(live_set_, 0);
    rebind_runner();
    // Same as the upload path: a restored set brings its timers with it, and a
    // board that comes up self-driving runs them from the first scan.
    (void)timers_.bind(&live_set_, now_us);
  }

  autorun_ = stored.autorun;
  // The one path into self-driving that no host asked for. It is deliberate:
  // this is the board that comes up on its own with nothing plugged into it,
  // which is the whole point of having a store. A record that says nothing
  // about autorun leaves the board exactly as it was -- waiting for a host.
  if (autorun_.enabled && have_set_) begin_autorun(autorun_, now_us);
  return SettingsError::None;
}

void HostLinkSession::on_save(uint16_t message_id) {
  if (settings_ == nullptr || !settings_->has_storage()) {
    send_error(message_id, "not_ready", "this board has nowhere to keep settings", "storage");
    return;
  }
  // A save erases and programs data flash, which on this part blocks for long
  // enough to cost scan periods -- tens of milliseconds against a 100 us scan.
  // Refused while a trial is running rather than quietly stalling the timing
  // authority in the middle of a response window.
  if (state_ == LinkState::Running) {
    send_error(message_id, "busy", "a trial is running", "trial");
    return;
  }

  StoredSettings stored;
  stored.wiring = wiring_;
  stored.autorun = autorun_;
  stored.set = &live_set_;
  stored.has_set = have_set_;

  // An erase cycle spent to change nothing is an erase cycle spent, and there is
  // a button in the web UI that invites being pressed twice. So the store is
  // compared against first -- streamed, a few bytes at a time, no second copy --
  // and a save that would write the same record writes nothing and says so.
  //
  // The counter does not move either: it counts writes to the part, which is
  // the number the endurance budget is about, and a "save" that wrote nothing
  // is not one of them.
  if (settings_->holds(stored, settings_write_count_)) {
    auto& unchanged = compose(statemachined_link_v1_DeviceMessage_saved_tag).body.saved;
    unchanged.has_set = have_set_;
    unchanged.set_version = have_set_ ? live_set_.version : 0;
    unchanged.autorun = autorun_.enabled;
    unchanged.write_count = settings_write_count_;
    unchanged.written = false;
    send(message_id);
    return;
  }

  const uint32_t next = settings_write_count_ + 1;
  if (!settings_->save(stored, next)) {
    // The store now holds no valid record, which the CRC turns into "this board
    // has forgotten" at the next boot rather than into something wrong. Saying
    // so is the point: a rig whose settings did not persist must not find out
    // after the power cut.
    send_error(message_id, "storage", "the settings were not written", "storage");
    return;
  }
  settings_write_count_ = next;

  auto& saved = compose(statemachined_link_v1_DeviceMessage_saved_tag).body.saved;
  saved.has_set = have_set_;
  saved.set_version = have_set_ ? live_set_.version : 0;
  saved.autorun = autorun_.enabled;
  // Flash wear, as a number somebody can see. About 100,000 erase cycles is the
  // budget on the reference board.
  saved.write_count = settings_write_count_;
  saved.written = true;
  send(message_id);
}

void HostLinkSession::on_configure(const link::Configure& m, uint16_t message_id,
                                   Microseconds now_us) {
  if (!have_set_) {
    send_error(message_id, "not_ready", "no graph set has been committed", "graph");
    return;
  }
  if (state_ == LinkState::Running) {
    send_error(message_id, "busy", "a trial is already running", "trial");
    return;
  }
  // One authority at a time. A host arming trials on a board that is also
  // arming its own would give two runs the same board and one of them the
  // wrong id; disabling autorun first is one message and says which of the two
  // is in charge.
  if (autorun_active_) {
    send_error(message_id, "busy", "the device is driving itself", "autorun");
    return;
  }

  const uint32_t trial_id = m.trial_id;
  // A set edit that did not land would otherwise leave the device confidently
  // running the old paradigms.
  if (m.set_version != live_set_.version) {
    send_error(message_id, "graph_mismatch", "the device holds a different set", "set_version");
    return;
  }

  // This is the switch. Every graph the session uses is already here, so
  // changing paradigm between two trials costs one field on a message the
  // device was going to receive anyway. See docs/developer/daemon.md 3.2.
  uint8_t graph_index = 0;
  if (!narrow_u8(m.graph_index, &graph_index)) {
    send_error(message_id, "bad_field", "graph_index", "graph_index");
    return;
  }
  if (graph_index >= live_set_.n_graphs) {
    send_error(message_id, "bad_index", "no graph in that slot", "graph_index");
    return;
  }

  const int32_t cap_ms = m.cap_ms;

  // Unspecified is serial, which is what a configure that says nothing about
  // starting has always meant.
  bool start_from_serial = true;
  bool start_from_line = false;
  switch (m.start) {
    case statemachined_link_v1_StartSource_START_SOURCE_UNSPECIFIED:
    case statemachined_link_v1_StartSource_START_SOURCE_SERIAL:
      break;
    case statemachined_link_v1_StartSource_START_SOURCE_LINE:
      start_from_serial = false;
      start_from_line = true;
      break;
    case statemachined_link_v1_StartSource_START_SOURCE_BOTH:
      start_from_line = true;
      break;
    default:
      send_error(message_id, "bad_field", "start must be serial, line or both", "start");
      return;
  }

  // Which line, and is it a line that could ever rise. Both refusals are here
  // rather than at the edge that never comes, because a trial armed on a line
  // the board will never see reports nothing at all: it simply waits, and the
  // host has no way to tell that from a subject who has not responded yet.
  uint8_t start_line = 0;
  if (start_from_line) {
    if (!m.has_start_line) {
      send_error(message_id, "bad_field", "start on a line needs start_line", "start_line");
      return;
    }
    if (!narrow_u8(m.start_line, &start_line)) {
      send_error(message_id, "bad_index", "no such input line", "start_line");
      return;
    }
    if (start_line >= identity_.input_line_count) {
      send_error(message_id, "bad_index", "no such input line", "start_line");
      return;
    }
    // The conditioner zeroes a disabled line, so its bit cannot rise no matter
    // what the pin does. `wiring` is refused while a trial is armed, so what is
    // checked here is still true when the edge is looked for.
    if ((wiring_.inputs.enable_mask & (static_cast<LineBitmask>(1) << start_line)) == 0) {
      send_error(message_id, "bad_index", "start_line is disabled in the wiring", "start_line");
      return;
    }
  }

  // Which timers this trial runs, if it says. The parallel to `graph_index`:
  // a trial type selects its timers the way it selects its graph, on a message
  // the device was going to receive anyway, so mapping a trial type onto a set
  // of timers costs no extra round trip in the inter-trial interval. Absent
  // means the device's own mask, set by `timers`.
  //
  // An override rather than a new setting, and reverted when the trial ends,
  // exactly like a distribution `patch` -- a mask that outlived its trial would
  // be a timer running, or not running, that nobody could account for
  // afterwards.
  const bool patch_timers = m.has_timers;
  const uint32_t trial_timers = m.timers;

  // Any previous trial's overrides come off before this one's go on, so a
  // trial that was armed and never started cannot leave its foreperiod behind.
  revert_distribution_patches();
  revert_timer_patch(now_us);
  if (!apply_distribution_patches(m, message_id)) return;  // refusal already sent

  runner_ = TrialRunner(live_set_, graph_index);
  rebind_runner();
  runner_.set_trial_cap_ms(cap_ms);
  if (patch_timers) {
    timers_patched_ = true;
    owe(pending_ops_, timers_.set_enabled(trial_timers, now_us));
  }

  armed_trial_id_ = trial_id;
  start_from_serial_ = start_from_serial;
  start_from_line_ = start_from_line;
  start_line_ = start_line;
  state_ = LinkState::Armed;

  // Both fields, always: this is the confirmation that start requires, and it
  // is not skippable.
  auto& armed = compose(statemachined_link_v1_DeviceMessage_armed_tag).body.armed;
  armed.trial_id = trial_id;
  armed.set_version = live_set_.version;
  armed.graph_index = graph_index;
  send(message_id);
}

void HostLinkSession::on_start(const link::Start& m, uint16_t message_id, Microseconds now_us) {
  const uint32_t trial_id = m.trial_id;
  // No trial runs that the device was not confirmed configured for.
  if (state_ != LinkState::Armed) {
    send_error(message_id, "not_ready", "not armed", "configure first");
    return;
  }
  if (trial_id != armed_trial_id_) {
    send_error(message_id, "unknown_trial", "armed for a different trial", "trial_id");
    return;
  }
  if (!start_from_serial_) {
    send_error(message_id, "not_ready", "this trial starts on a line, not on serial", "start");
    return;
  }

  // The run begins on the next advance_trial(), not here.
  //
  // It used to begin here, and the entry state's outputs were owed to the next
  // scan while its `entered_us` was stamped from `now_us` -- the timestamp
  // service_link() took off the link *before* it raised the EngineHold. Those
  // are not the same instant. Everything this function does afterwards, plus
  // serialising the reply below, is scan-deferred time that landed inside the
  // entry state's measured duration: **1 117 us of it**, measured on an Uno R4
  // Minima, on a state with no actions and a 0 ms timeout. Every trial's first
  // state reported about a millisecond too long, and its entry action reached
  // the pin about a millisecond after the timestamp that claimed it had. See
  // docs/operations/hardware.md, "Response latency and duration accuracy", for
  // the decomposition that found it -- the same wait mid-trial is 228 us.
  //
  // Deferring fixes it by construction rather than by being careful: the scan
  // that stamps `entered_us` is the scan that drives the entry action's pins,
  // because it is one call. That is exactly how autorun's first run has always
  // worked (begin_autorun(), and start_autorun_trial() below), and this is now
  // the same shape -- there is one thread of control that starts trials and
  // drives pins, and it is the timer's.
  start_pending_ = true;
  state_ = LinkState::Running;

  auto& started = compose(statemachined_link_v1_DeviceMessage_started_tag).body.started;
  started.trial_id = armed_trial_id_;
  // When the command was accepted, which is not when the trial starts: the run
  // begins on the next scan, together with its pins. A host that needs the
  // trial's own clock reads `entered_us` on the first row of the result, which
  // is the timestamp the machine actually ran on. See the note above.
  started.at_us = now_us;
  // Which source started it, so a host reads one field in both cases rather
  // than inferring the answer from whether the message carried an in_reply_to.
  started.by = statemachined_link_v1_StartSource_START_SOURCE_SERIAL;
  send(message_id);
}

void HostLinkSession::on_cancel(const link::Cancel& m, uint16_t message_id,
                                Microseconds now_us) {
  const uint32_t trial_id = m.trial_id;
  // Refused rather than acked, so the bridge learns that nothing was cancelled.
  if (state_ != LinkState::Running || trial_id != armed_trial_id_) {
    send_error(message_id, "unknown_trial", "no such trial is running", "trial_id");
    return;
  }

  // Reason is checked but only `host` is legal from the host; the others are
  // the device's own account of why it stopped. Unspecified is `host`.
  if (m.reason != statemachined_link_v1_CancelReason_CANCEL_REASON_UNSPECIFIED &&
      m.reason != statemachined_link_v1_CancelReason_CANCEL_REASON_HOST) {
    send_error(message_id, "bad_field", "only host is a reason the host may give", "reason");
    return;
  }

  // A cancel that beat the scan cancels a trial that does not exist yet, so
  // start it first: see start_now_if_pending(). Its entry actions are owed to
  // the pins exactly as they would have been, and the cancel below takes them
  // straight back down.
  owe(pending_ops_, start_now_if_pending(now_us));

  // Loses the race against a graph that already reached a terminal state. The
  // bridge must cope with asking to cancel and being told Hit -- the
  // alternative is a record claiming a trial was cancelled when the animal had
  // already responded.
  const bool cancelled = runner_.cancel(TrialCancelReason::Host, now_us);

  auto& ack = compose(statemachined_link_v1_DeviceMessage_cancel_ack_tag).body.cancel_ack;
  ack.trial_id = trial_id;
  ack.cancelled = cancelled;
  ack.outcome = static_cast<int32_t>(runner_.result().outcome);
  send(message_id);
}

bool HostLinkSession::apply_distribution_patches(const link::Configure& m,
                                                 uint16_t message_id) {
  for (pb_size_t k = 0; k < m.patch_count; ++k) {
    const statemachined_link_v1_Patch& entry = m.patch[k];
    if (entry.i >= live_set_.n_distributions) {
      send_error(message_id, "bad_index", "no distribution has that index", "patch");
      revert_distribution_patches();
      return false;
    }
    // Cannot trip while link.options and config.h agree, which a static_assert
    // holds -- nanopb refuses a longer list before this is reached. Kept so
    // that the bound is written where the array is indexed.
    if (n_patched_distributions_ >= kMaxPatchedDistributions) {
      send_error(message_id, "too_many", "max_patched_distributions", "patch");
      revert_distribution_patches();
      return false;
    }

    // The inverse of the patch, not the patch: what to put back when the trial
    // ends. Saved before anything is written, so a refusal below still reverts
    // cleanly.
    RandomDistribution& distribution = live_set_.distributions[entry.i];
    PatchedDistribution& saved = patched_distributions_[n_patched_distributions_++];
    saved.index = static_cast<RandomDistributionIndex>(entry.i);
    saved.a = distribution.a;
    saved.b = distribution.b;
    saved.c = distribution.c;

    // Only a, b and c. `kind` may not be patched: that would change the shape
    // of the draw, which is a different graph and a different set_version.
    if (entry.has_a) distribution.a = entry.a;
    if (entry.has_b) distribution.b = entry.b;
    if (entry.has_c) distribution.c = entry.c;
  }
  return true;
}

void HostLinkSession::revert_distribution_patches() {
  for (uint8_t i = 0; i < n_patched_distributions_; ++i) {
    const PatchedDistribution& saved = patched_distributions_[i];
    RandomDistribution& distribution = live_set_.distributions[saved.index];
    distribution.a = saved.a;
    distribution.b = saved.b;
    distribution.c = saved.c;
  }
  n_patched_distributions_ = 0;
}

void HostLinkSession::on_ping(uint16_t message_id, Microseconds now_us) {
  if (!have_boot_) {
    booted_us_ = now_us;
    have_boot_ = true;
  }
  auto& pong = compose(statemachined_link_v1_DeviceMessage_pong_tag).body.pong;
  pong.up_us = since(booted_us_, now_us);
  // The device clock itself, raw, wrapping every ~71 minutes. `up_us` counts
  // from the first time anything asked, which is a different origin on every
  // session and cannot be compared with the `entered_us` in a result. This is
  // the value a host correlates against its own clock -- see docs/developer/daemon.md 4.5,
  // where that correlation is called load-bearing.
  pong.us = now_us;
  send(message_id);
}

void HostLinkSession::on_state_request(uint16_t message_id, Microseconds now_us) {
  if (!have_boot_) {
    booted_us_ = now_us;
    have_boot_ = true;
  }
  auto& report =
      compose(statemachined_link_v1_DeviceMessage_state_report_tag).body.state_report;
  report.link_state = static_cast<uint32_t>(state_);
  report.has_graph = true;
  report.graph.has_set = have_set_;
  report.graph.set_version = have_set_ ? live_set_.version : 0;
  report.graph.n_graphs = have_set_ ? live_set_.n_graphs : 0;
  report.graph.index = runner_.graph_index();
  report.has_wiring = have_wiring_;
  // Whether this board is arming its own trials. `link_state` already says
  // Relighting during the dwell between two of them, but not while one is in
  // flight -- and "who started this trial" is exactly what a daemon that has
  // just connected to a rig needs to know.
  report.autorun = autorun_active_;
  report.trial_id = armed_trial_id_;
  // `running` is the answer to "did the host's start take", not "has the engine
  // ticked yet": between `started` going out and the scan that begins the run
  // there is up to one scan period in which the runner has not started and the
  // trial unarguably has. Reporting false there would make a state_report sent
  // straight after a start contradict the `started` it just received.
  report.running = start_pending_ || runner_.running();
  report.current_state = start_pending_ ? entry_state_of_live_graph() : runner_.current_state();
  report.up_us = since(booted_us_, now_us);
  // Diagnosis, not control: a link dropping frames should be visible to
  // whoever is debugging the rig rather than inferred from trials that did not
  // happen.
  report.dropped_lines = reader_.dropped();
  report.bad_lines = bad_lines_;
  // The live pins. This is the only way anything outside the device can check
  // that a graph's line numbers land on the pins somebody actually wired: there
  // is no read-back path from a pin, and `out` is the engine's own shadow
  // rather than a measurement. Under emulation it is what makes the pin map
  // testable at all.
  report.has_io = true;
  report.io.in = last_word_;
  report.io.out = runner_.driven_levels();
  report.has_scan = true;
  report.scan.hz = scan_.hz;
  report.scan.overruns = scan_.overruns;
  report.scan.worst_gap = scan_.worst_gap;
  report.scan.tx_stalls = scan_.tx_stalls;
  // Same question as the counters above it -- is this device keeping up.
  report.scan.visits_dropped = dropped_visits_;
  // `enabled` is the mask in force *now*, so during a trial that overrode it
  // this is the trial's, not the device's. `running` is one bit per timer
  // actually in flight, delay included -- a timer counting down its onset is
  // running, it is simply not high yet.
  report.scan.timers_enabled = timers_.enabled();
  report.scan.timers_running = timers_.bits() >> kFirstTimerLine;
  send(message_id);
}

// ------------------------------------------------------------ trial loop ---

OutputUpdate HostLinkSession::advance_trial(LineBitmask word, Microseconds now_us) {
  if (!have_boot_) {
    booted_us_ = now_us;
    have_boot_ = true;
  }
  // Anything a cancel or a fail-safe owed since the last scan.
  OutputUpdate owed = pending_ops_;
  pending_ops_ = OutputUpdate{};

  // The global timers, before anything reads the word -- including before
  // `last_word_` is written, so that the timer bits are part of the word every
  // edge on this device is measured against. A timer bit that joined the word
  // after that snapshot would look like a rising edge on every scan it was
  // high, which would make a timer unusable as a trial's start line.
  //
  // They tick whether or not a trial is running: that is what makes them
  // global, and it is why they are here rather than inside the machine, which
  // is rebuilt per trial. Their bits join the input word as ordinary lines, so
  // a transition waiting on a timer is a transition waiting on a line, and
  // needs nothing special anywhere below this point. See
  // machine/global_timers.h.
  const TimerTick timers = timers_.tick(word, now_us);
  owe(owed, timers.ops);
  word |= timers.bits;
  // A timer a state action started or cancelled on the *previous* scan, after
  // the machine had already built its update. Merged here so it is paid exactly
  // once, on the scan after the one that owed it.
  owe(owed, timer_action_ops_);
  timer_action_ops_ = OutputUpdate{};

  // A pulse raised by the last act of a trial -- the ordinary way to write a
  // reward -- falls due after the run has ended and the session is back to
  // Idle. Returning an empty update here would leave the valve open until the
  // next trial started.
  const LineBitmask previous_word = last_word_;
  last_word_ = word;

  // A start the link accepted since the last scan. Here, so that the trial's
  // entered_us and the pins its entry action drives are one instant -- see
  // on_start(). Not evaluated in this same tick: a state entered by a
  // transition gets its first look on the following scan, and the entry state
  // of a trial is not a special case.
  if (start_pending_) {
    start_pending_ = false;
    // Pulses owed from before this trial, which start() is about to drop. The
    // autorun path gets these from the idle branch below; this one has to ask.
    owe(owed, runner_.service_outputs(now_us));
    owe(owed, runner_.start(armed_trial_id_, session_seed_, now_us, word));
    return owed;
  }

  // A trial armed to start on a line, and the edge that starts it.
  //
  // An *edge*, not a level: a line already asserted when `configure` arrived
  // would otherwise start the trial on the very next scan, which is not a start
  // signal but a line nobody had lowered yet. So the bit must be seen low on one
  // scan and high on the next, and `previous_word` is what makes that a
  // question about the world rather than about when the host happened to send
  // the command. The word is the conditioned one, so `invert` has already been
  // applied and a debounce, if the line has one, has already been waited out --
  // the edge here is the edge the paradigm's own transitions would see.
  //
  // Started here, in the trial loop, rather than flagged for the link the way a
  // serial start is flagged for the scan: this *is* the scan. The edge, the
  // trial's `entered_us` and the pins its entry action drives are one call, one
  // instant, and one scan period after the pin moved -- there is no equivalent
  // of on_start()'s 1 117 us to defer away, because no foreground work is in
  // the path at all. Telling the host is the part that waits (see
  // `line_start_pending_`), and it waits behind the trial rather than in it.
  if (state_ == LinkState::Armed && start_from_line_) {
    const LineBitmask bit = static_cast<LineBitmask>(1) << start_line_;
    if ((word & bit) != 0 && (previous_word & bit) == 0) {
      // One edge starts one trial. Cleared before the run so that the line
      // going high again mid-trial is just an input, which is what it is: this
      // flag is the arming, and the arming is spent.
      start_from_line_ = false;
      state_ = LinkState::Running;
      line_start_pending_ = true;
      line_started_at_us_ = now_us;
      // Pulses owed from before this trial, which start() is about to drop --
      // the same debt the serial path collects above.
      owe(owed, runner_.service_outputs(now_us));
      owe(owed, runner_.start(armed_trial_id_, session_seed_, now_us, word));
      return owed;
    }
  }

  if (state_ != LinkState::Running) {
    OutputUpdate idle = runner_.service_outputs(now_us);
    // The dwell a terminal state declared has run out, so another run begins.
    // Here rather than in the link's thread of control for the reason every
    // other output on this device is: the scan drives the pins.
    // Not while a result is still waiting to go out: starting the next run
    // would reset the record that result is built from. One pass of the
    // foreground is all it waits for.
    if (state_ == LinkState::Relighting && !result_pending_ &&
        reached(relight_at_us_, now_us)) {
      const OutputUpdate entry_ops = start_autorun_trial(word, now_us);
      idle.set_high = (idle.set_high | entry_ops.set_high) & ~entry_ops.set_low;
      idle.set_low = (idle.set_low | entry_ops.set_low) & ~entry_ops.set_high;
    }
    idle.set_high = (owed.set_high | idle.set_high) & ~idle.set_low;
    idle.set_low = (owed.set_low | idle.set_low) & ~idle.set_high;
    return idle;
  }

  OutputUpdate ops = runner_.advance(word, now_us);
  ops.set_high = (owed.set_high | ops.set_high) & ~ops.set_low;
  ops.set_low = (owed.set_low | ops.set_low) & ~ops.set_high;
  if (!runner_.running()) {
    // The host learns of an outcome without having to ask for it. A trial that
    // ended silently would be indistinguishable from a hung one. Under autorun
    // there may be nobody to hear it, which changes nothing: the result is the
    // device's account of what it did, and a board that stopped writing them
    // down when the port closed would be a board whose record depended on who
    // was watching.
    // Flagged, not sent. Sending it means building a result_begin, however many
    // result_path chunks and a result_end -- the largest burst of formatting on
    // this device -- and doing that here means doing it in the trial loop, which
    // on a board is the timer ISR. ReplySink::send_frame() spins when the
    // transmit queue is full, and a spin in an interrupt waiting for the
    // foreground that drains that queue does not end. drain_outbound() sends it,
    // from the foreground, after the visits it summarises.
    result_pending_ = true;
    // Every timer that declared itself trial-bound stops here, and the rest run
    // on. Per timer, as VStim's ResetTimer() is, rather than a property of the
    // device -- see graph/global_timer.h.
    owe(ops, timers_.end_of_run(now_us));
    // And this trial's enable mask goes back to the device's, alongside the
    // distribution patches below and for the same reason.
    revert_timer_patch(now_us);
    // Safe to revert before the result is built rather than after: emit_result()
    // reads the trial record and the run record, and touches the distribution
    // pool nowhere. The drawn durations it reports were written down when they
    // were drawn.
    revert_distribution_patches();

    // The graph says when another run may begin; whether anything acts on it is
    // this. A dwell of kNoRelight -- a terminal state that declares none, or a
    // run that was cancelled and so reached no terminal state at all -- stops
    // the board, which is how a paradigm says "this outcome ends the session".
    const Milliseconds dwell = runner_.run_record().relight_ms;
    if (autorun_active_ && dwell >= 0) {
      state_ = LinkState::Relighting;
      relight_at_us_ = now_us + static_cast<Microseconds>(dwell) * 1000u;
    } else {
      autorun_active_ = false;
      state_ = LinkState::Idle;
    }
  }
  return ops;
}

StateIndex HostLinkSession::entry_state_of_live_graph() const {
  if (!have_set_ || runner_.graph_index() >= live_set_.n_graphs) return kNoState;
  const GraphEntry& g = live_set_.graphs[runner_.graph_index()];
  if (g.entry == kNoState) return kNoState;
  // Per-graph, like every index that crosses the wire: the pool offset is the
  // device's business. Same arithmetic StateMachine::get_current_state_index()
  // does, and for the same reason.
  return static_cast<StateIndex>(g.entry - g.first_state);
}

OutputUpdate HostLinkSession::start_now_if_pending(Microseconds now_us) {
  OutputUpdate ops;
  if (!start_pending_) return ops;
  start_pending_ = false;
  // `last_word_` rather than a fresh read, because this is the link's thread of
  // control and it reads no pin. It is the previous scan's word, at most one
  // period old, and it is what the transitions are armed against -- the same
  // thing the scan would have handed start() had it got there first.
  ops = runner_.start(armed_trial_id_, session_seed_, now_us, last_word_);
  return ops;
}

OutputUpdate HostLinkSession::start_autorun_trial(LineBitmask word, Microseconds now_us) {
  // Its own id, counted on the device, because there is no host to assign one.
  // The per-trial stream is derived from it exactly as a host-driven trial's
  // is, so an unattended session replays from the stored seed alone.
  armed_trial_id_ = autorun_next_trial_id_++;
  state_ = LinkState::Running;
  return runner_.start(armed_trial_id_, autorun_.seed, now_us, word);
}

OutputUpdate HostLinkSession::link_lost(Microseconds now_us) {
  OutputUpdate ops;
  // A board that was explicitly told to drive itself is doing what it was asked
  // to do, and the port closing is not news to it: an unplugged cable is the
  // expected end of the "upload a paradigm, then detach" workflow, not a
  // failure. Nothing is cancelled and nothing fails safe -- which is why
  // enabling autorun takes a command of its own rather than being something a
  // dropped link can arrive at by accident.
  //
  // The reader and the duplicate guard still reset, because those belong to the
  // session that has just ended rather than to the run that is still going.
  if (autorun_active_) {
    reader_.reset();
    guard_.forget();
    builder_.abandon();
    greeted_ = false;
    return ops;
  }
  pending_ops_ = OutputUpdate{};
  if (state_ == LinkState::Running) {
    // As in on_cancel(): a run the scan has not reached yet is still a run this
    // has to end and report. Its entry outputs are discarded rather than owed,
    // because every line is about to go to its safe level anyway.
    start_now_if_pending(now_us);
    runner_.cancel(TrialCancelReason::LinkLost, now_us);
    // cancel() hands its outputs to the next scan rather than returning them,
    // because a cancel normally arrives between scans. There is not going to be
    // a next scan of this trial, so collect them here.
    ops = runner_.advance(0, now_us);
  }
  // Back to Idle, not Greeting: the committed graph survives a reconnect, so a
  // bridge that comes back does not have to re-upload one. It sends hello
  // anyway, which is what brings a fresh session seed.
  if (state_ != LinkState::Greeting) state_ = LinkState::Idle;
  // Whatever is still queued was for a host that is gone, exactly like the
  // transmit queue main.cpp clears alongside this.
  visit_head_ = visit_tail_ = 0;
  // The cancel above ended the run and flagged its result, and there is nobody
  // to send it to -- main.cpp clears the transmit queue alongside this for the
  // same reason. A board driving itself returns earlier and keeps both.
  result_pending_ = false;
  start_pending_ = false;
  // The arming and its unsent announcement both belonged to the session that
  // just ended. A board left armed on a line across a reconnect would start a
  // trial the returning host never asked for.
  start_from_line_ = false;
  line_start_pending_ = false;
  greeted_ = false;
  revert_distribution_patches();
  armed_trial_id_ = 0;
  builder_.abandon();
  reader_.reset();
  guard_.forget();
  return ops;
}

OutputUpdate HostLinkSession::fail_safe() {
  OutputUpdate ops;
  pending_ops_ = OutputUpdate{};
  // Every timer stops. Without this a free-running one would re-raise its line
  // on its next pulse and fight the safe levels -- and this runs when the graph
  // is what may be wrong, which includes the timer that graph declared. The
  // ops it returns are discarded on purpose: the safe levels below drive every
  // line the board has, so they already say where each one goes.
  (void)timers_.set_enabled(0, 0);
  timers_enabled_ = 0;
  timers_patched_ = false;
  // The wiring's, not a graph's, and applied whether or not a graph exists --
  // which is the whole point of the move. A rig image holds no graph at reset,
  // and this is the call main.cpp makes before the first scan.
  const LineBitmask safe = wiring_.output_safe_levels;
  ops.set_high = safe;
  // Every line the board has, not only the ones some state raised: this runs
  // when the graph may be the thing that is wrong.
  const LineBitmask all = (identity_.output_line_count >= 32)
                              ? 0xFFFFFFFFu
                              : ((1u << identity_.output_line_count) - 1u);
  ops.set_low = all & ~safe;
  // The engine's shadow of the levels is now wrong unless it is told. A Toggle
  // is the only action whose meaning depends on where the line already was.
  runner_.set_initial_levels(ops.set_high);
  return ops;
}

void HostLinkSession::record_visit(const StateVisit& v, uint32_t seq) {
  // On a board this is the scan ISR, so it does the least it can: a copy and an
  // index bump. Everything with a cost in it -- the JSON, the CRC, the queue --
  // is drain_visits(), in the foreground. See the note on that declaration for
  // what it cost when it was here.
  const uint8_t next = static_cast<uint8_t>((visit_head_ + 1) % kVisitRingDepth);
  if (next == visit_tail_) {
    // Full. The newest is dropped rather than the oldest, so that the producer
    // never touches `tail` and the ring stays lock-free with an interrupt at
    // one end. The host sees the gap in `seq` either way, and the count goes
    // out in state_report.
    ++dropped_visits_;
    return;
  }
  PendingVisit& slot = visit_ring_[visit_head_];
  slot.visit = v;
  slot.seq = seq;
  // Zero where there is no host-configured trial -- the bench, a line-started
  // run before anything assigned an id. The trace is still worth having; it
  // simply joins to nothing.
  slot.trial_id = (state_ == LinkState::Running) ? armed_trial_id_ : 0;
  visit_head_ = next;
}

void HostLinkSession::drain_outbound(uint8_t max_visit_lines) {
  // Before this trial's visits, because it is the line that says which trial
  // they belong to and a host that saw them first would be reading a stream it
  // had not been told had begun.
  if (line_start_pending_) {
    line_start_pending_ = false;
    emit_line_started();
  }
  drain_visits(max_visit_lines);
  // Only once the visits are out: a result summarises them, and a host reading
  // the stream in order should not meet the summary first. The ring being empty
  // is the condition, not the count sent, so a paced caller takes as many passes
  // as it needs and the result still follows.
  if (result_pending_ && visit_tail_ == visit_head_) {
    result_pending_ = false;
    emit_result();
  }
}

void HostLinkSession::emit_line_started() {
  // No in_reply_to: nothing asked. This is the device reporting an event, in
  // the same class as `visit` and `result`.
  auto& started = compose(statemachined_link_v1_DeviceMessage_started_tag).body.started;
  started.trial_id = armed_trial_id_;
  // Unlike the serial path's, this `at_us` *is* the start of the trial and not
  // an acknowledgement of a command: it is the timestamp of the scan that saw
  // the edge, which is the timestamp that scan stamped `entered_us` with. See
  // on_start() for why the two cases differ.
  started.at_us = line_started_at_us_;
  started.by = statemachined_link_v1_StartSource_START_SOURCE_LINE;
  started.has_line = true;
  started.line = start_line_;
  send_unsolicited();
}

void HostLinkSession::drain_visits(uint8_t max_lines) {
  for (uint8_t sent = 0; visit_tail_ != visit_head_; ++sent) {
    if (max_lines != 0 && sent == max_lines) return;
    const PendingVisit& p = visit_ring_[visit_tail_];
    // Emitted when the state is LEFT, not when it is entered: a visit's
    // duration and exit cause do not exist before then. Being sent a moment
    // later than that is not a latency problem, because what matters is the
    // timestamp and `entered_us` is exact -- and each exit tells a host both
    // when the state it reports ended and, via the transition it resolves
    // against the graph, which state the machine is in now.
    auto& visit = compose(statemachined_link_v1_DeviceMessage_visit_tag).body.visit;
    visit.trial_id = p.trial_id;
    visit.seq = p.seq;
    // The same row as a result_path entry, decoded by the same function on the
    // host. Two shapes for one fact is how the two drift apart.
    visit.has_v = true;
    visit.v = visit_row(p.visit);
    send_unsolicited();
    // Last, so the producer never sees a slot freed before it has been read.
    visit_tail_ = static_cast<uint8_t>((visit_tail_ + 1) % kVisitRingDepth);
  }
}

void HostLinkSession::emit_result() {
  const TrialRecord& r = runner_.result();
  const StateMachineRunRecord& run = runner_.run_record();

  // Over the protobuf of result_begin and every result_path, as they are sent.
  uint16_t checksum = 0xFFFF;

  {
    auto& begin =
        compose(statemachined_link_v1_DeviceMessage_result_begin_tag).body.result_begin;
    begin.trial_id = r.trial_id;
    begin.outcome = static_cast<int32_t>(r.outcome);
    begin.cancel_reason = static_cast<uint32_t>(r.cancel_reason);
    begin.total_us = run.total_us;
    begin.path_len = run.path_len;
    // What the host needs to know which window it received. `truncated` says a
    // path overflowed; these say by how much and where the surviving one
    // starts, so a trace assembled from the visit stream can be reconciled
    // against it rather than merely compared for length.
    begin.first_seq = run.first_seq();
    begin.total_visits = run.total_visits;
    begin.truncated = run.path_truncated;
    const size_t len = encode_composed();
    checksum = crc16_ccitt(payload_, len, checksum);
    frame_and_send(len);
  }

  // Chunked for the same reason the upload is: a full path does not fit in one
  // frame, and buffering one that did would cost a kilobyte this board does
  // not have. A chunk carries as many rows as link.options gives it, which is
  // sized so that even the widest rows fit a frame.
  uint8_t from = 0;
  while (from < run.path_len) {
    auto& chunk = compose(statemachined_link_v1_DeviceMessage_result_path_tag).body.result_path;
    chunk.trial_id = r.trial_id;
    chunk.from = from;
    uint8_t i = from;
    while (i < run.path_len && chunk.p_count < kResultRowsPerFrame) {
      // visit(), not path[i]: the ring drops from the front, so slot 0 is not
      // visit 0 once it has wrapped. `from` stays an offset into what was sent.
      chunk.p[chunk.p_count++] = visit_row(run.visit(i));
      ++i;
    }
    const size_t len = encode_composed();
    checksum = crc16_ccitt(payload_, len, checksum);
    frame_and_send(len);
    from = i;
  }

  auto& end = compose(statemachined_link_v1_DeviceMessage_result_end_tag).body.result_end;
  end.trial_id = r.trial_id;
  // Catches a dropped chunk, which no per-frame crc can see: the frame that
  // vanished was perfectly well formed.
  end.checksum = checksum;
  send_unsolicited();
}

// --------------------------------------------------------------- sending ---

namespace {

/// The body every refusal shares, whether or not it can name a `message_id`.
void write_error_body(statemachined_link_v1_Error& error, const char* code, const char* message,
                      const char* context) {
  put_text(error.code, code);
  put_text(error.message, message);
  // Every refusal names what to change; an empty context is a defect here
  // rather than a terse style.
  put_text(error.context, (context != nullptr && context[0] != '\0') ? context : code);
}

}  // namespace

void HostLinkSession::send_error(uint16_t message_id, const char* code, const char* message,
                                 const char* context) {
  write_error_body(compose(statemachined_link_v1_DeviceMessage_error_tag).body.error, code,
                   message, context);
  send(message_id);
}

// Split from send_error rather than folded into it behind a sentinel value.
// Zero is a perfectly ordinary message_id -- the counter is a u16 that wraps
// through it -- so a `message_id != 0` test here would answer a real command
// with a reply carrying no `in_reply_to` and remember nothing for the retry
// guard, which is precisely the command a bridge would resend and precisely
// the resend that must not re-execute. Whether a message_id was read is a fact
// about the frame, and the only thing that can know it is the caller.
void HostLinkSession::send_orphan_error(const char* code, const char* message,
                                        const char* context) {
  write_error_body(compose(statemachined_link_v1_DeviceMessage_error_tag).body.error, code,
                   message, context);
  send_unsolicited();
}

void HostLinkSession::send_ack(uint16_t message_id) {
  compose(statemachined_link_v1_DeviceMessage_ack_tag);
  send(message_id);
}

link::DeviceMessage& HostLinkSession::compose(pb_size_t which) {
  tx_ = kNoDeviceMessage;
  tx_.which_body = which;
  return tx_;
}

size_t HostLinkSession::encode_composed() {
  tx_.message_id = tx_message_id_;
  pb_ostream_t stream = pb_ostream_from_buffer(payload_, sizeof(payload_));
  if (!pb_encode(&stream, statemachined_link_v1_DeviceMessage_fields, &tx_)) return 0;
  return stream.bytes_written;
}

size_t HostLinkSession::frame_and_send(size_t payload_len) {
  // A message that did not encode is a firmware bug and not a wire condition:
  // the static_asserts at the top of this file hold every message to the
  // frame. Nothing half-built goes out. (No message is empty: the body's tag
  // is always there, even for an `ack` that carries nothing.)
  if (payload_len == 0) return 0;
  const size_t n = encode_frame(payload_, payload_len, frame_);
  if (n == 0) return 0;
  ++tx_message_id_;
  out_.send_frame(frame_, n);
  return n;
}

void HostLinkSession::send(uint16_t message_id) {
  tx_.has_in_reply_to = true;
  tx_.in_reply_to = message_id;
  const size_t n = frame_and_send(encode_composed());
  if (n == 0) return;
  guard_.remember(message_id, frame_, n);
}

void HostLinkSession::send_unsolicited() { frame_and_send(encode_composed()); }

}  // namespace statemachined
