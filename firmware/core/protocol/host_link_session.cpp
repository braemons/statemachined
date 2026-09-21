// SPDX-License-Identifier: GPL-3.0-or-later
#include "protocol/host_link_session.h"

#include "protocol/crc16.h"
#include "protocol/msg_type.h"

namespace statemachined {
namespace {

constexpr uint16_t kProtocolVersion = 1;

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

const char* cause_name(StateExitCause c) {
  switch (c) {
    case StateExitCause::Timeout:
      return "timeout";
    case StateExitCause::Transition:
      return "transition";
    case StateExitCause::Cancel:
      return "cancel";
    case StateExitCause::Terminal:
      return "terminal";
  }
  return "terminal";
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
      return "bad_json";
    case UploadError::CountMismatch:
    case UploadError::ChecksumMismatch:
    case UploadError::Invalid:
      return "bad_graph";
  }
  return "internal";
}

}  // namespace

void DuplicateCommandGuard::remember(uint16_t message_id, const char* line, size_t n) {
  if (n > sizeof(reply_)) {  // cannot happen: everything is built in a kMaxLine buffer
    have_ = false;
    return;
  }
  for (size_t i = 0; i < n; ++i) reply_[i] = line[i];
  reply_len_ = n;
  last_message_id_ = message_id;
  have_ = true;
}

HostLinkSession::HostLinkSession(ReplySink& out, const DeviceIdentity& identity)
    : out_(out), identity_(identity), reader_(rx_, sizeof(rx_)), runner_(live_set_) {
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

void HostLinkSession::receive(const char* bytes, size_t n, Microseconds now_us) {
  for (size_t i = 0; i < n; ++i) receive_byte(bytes[i], now_us);
}

void HostLinkSession::receive_byte(char c, Microseconds now_us) {
  if (!reader_.feed(c)) return;

  if (reader_.status() != FrameError::None) {
    // The line never assembled, so there is no message_id to attribute the error to.
    // Reported anyway rather than dropped: a host waiting for an answer it will
    // never get is worse than one told its line was unusable.
    ++bad_lines_;
    const char* code = reader_.status() == FrameError::TooLong ? "too_long" : "bad_json";
    send_orphan_error(code, frame_error_str(reader_.status()), "line");
    return;
  }
  handle_line(reader_.line(), reader_.len(), now_us);
}

void HostLinkSession::handle_line(const char* line, size_t n, Microseconds now_us) {
  Frame f;
  const FrameError fe = verify_frame(line, n, &f);
  if (fe != FrameError::None) {
    // Verified before parsed, always. A half-parsed graph_state acted on in
    // part is the failure the whole line layer exists to prevent.
    ++bad_lines_;
    const char* code = fe == FrameError::BadCrc ? "bad_crc" : "bad_json";
    send_orphan_error(code, frame_error_str(fe), "line");
    return;
  }

  JsonObject m(line, n);
  if (!m.valid()) {
    ++bad_lines_;
    send_orphan_error("bad_json", json_error_str(m.error()), "line");
    return;
  }

  uint16_t message_id = 0;
  if (!m.u16(kMessageIdKey, &message_id)) {
    send_orphan_error("bad_json", "no message_id", "message_id");
    return;
  }

  // A retry of the command just answered gets that answer back verbatim and
  // changes nothing. Acting twice is what matters: a re-executed start would
  // run a second trial.
  if (guard_.is_repeat(message_id)) {
    out_.send_line(guard_.reply(), guard_.reply_len());
    return;
  }

  dispatch(m, JsonSpan{f.covered, f.covered_len}, message_id, now_us);
}

void HostLinkSession::dispatch(const JsonObject& m, JsonSpan covered, uint16_t message_id,
                               Microseconds now_us) {
  JsonSpan name;
  if (!m.str(kMsgTypeKey, &name)) {
    send_error(message_id, "bad_json", "no message type", kMsgTypeKey);
    return;
  }
  const MsgType t = msg_type_from(name);

  // hello is the only thing accepted before a hello: everything else needs a
  // session seed, and a device answering commands without one would be running
  // trials nobody could replay.
  // Not `state_ == Greeting`: a board driving itself from stored settings is
  // Running with nobody having greeted it, and it must still refuse every
  // command from a host that skipped the handshake.
  if (!greeted_ && t != MsgType::Hello) {
    send_error(message_id, "not_ready", "no hello yet", "hello");
    return;
  }

  // A switch rather than a chain of comparisons, so that a message type added
  // to MsgType and forgotten here is a -Wswitch warning at build time instead
  // of a command refused as unknown_type on somebody's bench.
  switch (t) {
    case MsgType::Hello:
      return on_hello(m, message_id, now_us);
    case MsgType::Ping:
      return on_ping(message_id, now_us);
    case MsgType::State:
      return on_state_request(message_id, now_us);
    case MsgType::Wiring:
      return on_wiring(m, message_id);
    case MsgType::Timers:
      return on_timers(m, message_id, now_us);
    case MsgType::Pins:
      return on_pins_request(m, message_id);
    case MsgType::Autorun:
      return on_autorun(m, message_id, now_us);
    case MsgType::Save:
      return on_save(message_id);
    case MsgType::Configure:
      return on_configure(m, message_id, now_us);
    case MsgType::Start:
      return on_start(m, message_id, now_us);
    case MsgType::Cancel:
      return on_cancel(m, message_id, now_us);

    case MsgType::SetBegin:
    case MsgType::SetEnd:
    case MsgType::GraphBegin:
    case MsgType::GraphDist:
    case MsgType::GraphState:
    case MsgType::GraphTransition:
    case MsgType::GraphAction:
    case MsgType::GraphTimer:
    case MsgType::GraphEnd:
      return on_upload_message(m, covered, message_id, now_us, t);

    // Names this device knows but only ever sends. A host saying `pong` is as
    // unrecognisable a command as one saying `teleport`, and is refused the
    // same way rather than falling through to something that half-works.
    case MsgType::HelloAck:
    case MsgType::Ack:
    case MsgType::SetOk:
    case MsgType::Armed:
    case MsgType::Started:
    case MsgType::CancelAck:
    case MsgType::ResultBegin:
    case MsgType::ResultPath:
    case MsgType::ResultEnd:
    case MsgType::Event:
    case MsgType::Error:
    case MsgType::Log:
    case MsgType::Pong:
    case MsgType::StateReport:
    case MsgType::Visit:
    case MsgType::PinMap:
    case MsgType::AutorunOk:
    case MsgType::Saved:
    case MsgType::Unknown:
      break;
  }

  send_error(message_id, "unknown_type", "unrecognised message type", kMsgTypeKey);
}

// -------------------------------------------------------------- handlers ---

void HostLinkSession::on_hello(const JsonObject& m, uint16_t message_id, Microseconds now_us) {
  uint16_t proto = 0;
  if (!m.u16("proto", &proto)) {
    send_error(message_id, "bad_json", "no proto", "proto");
    return;
  }
  if (proto != kProtocolVersion) {
    send_error(message_id, "bad_proto", "this firmware does not speak that version", "proto");
    return;
  }
  uint64_t seed = 0;
  if (!m.hex64("seed", &seed)) {
    send_error(message_id, "bad_json", "seed must be a hex string", "seed");
    return;
  }

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

  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::HelloAck), tx_message_id_);
  w.in_reply_to(message_id);
  w.key_u32("proto", kProtocolVersion);
  w.key_str("board", identity_.board);
  w.key_str("fw", identity_.firmware_version);
  w.key_u32("n_input_lines", identity_.input_line_count);
  w.key_u32("n_output_lines", identity_.output_line_count);
  w.key_u32("scan_hz", identity_.measured_scan_hz);
  // Nested rather than flat, and not for tidiness: a flat hello_ack has
  // nineteen members, and the reader's limit is what bounds JsonObject's stack
  // footprint. Raising the limit to fit one device-to-host message would cost
  // every parse on the device, including the ones a trial waits on.
  w.begin_object("caps");
  w.key_u32("max_line", kMaxLine);
  w.key_u32("max_states", kMaxStates);
  w.key_u32("max_transitions", kMaxTransitions);
  w.key_u32("max_output_actions", kMaxOutputActions);
  w.key_u32("max_distributions", kMaxDistributions);
  w.key_u32("max_choice_options", kMaxChoiceOptions);
  w.key_u32("max_path", kMaxPath);
  w.key_u32("max_graphs", kMaxGraphs);
  w.key_u32("max_timers", kMaxTimers);
  // Which input line the first timer holds high. Not derivable from the line
  // count -- the timers are counted down from the top of the word so that a
  // graph means the same thing on a board with eight inputs and one with
  // twenty -- so a host that wants to write a predicate against a timer has to
  // be told. See config.h.
  w.key_u32("first_timer_line", kFirstTimerLine);
  w.end_object();
  w.key_bool("has_set", have_set_);
  w.key_u32("set_version", have_set_ ? live_set_.version : 0);
  w.key_u32("n_graphs", have_set_ ? live_set_.n_graphs : 0);
  // Whether anybody has told this board what it is wired to. False means it is
  // running the compile-time defaults, which a daemon needs to know before it
  // decides whether to push a wiring or to trust the one that is there.
  w.key_bool("has_wiring", have_wiring_);
  send(w, message_id);
}

void HostLinkSession::on_timers(const JsonObject& m, uint16_t message_id, Microseconds now_us) {
  // Refused mid-trial for the reason `wiring` is: this changes what the scan
  // does, and a timer switched off under a running trial would move a timing
  // that trial's record could not account for. A trial that wants a different
  // mask says so in its own `configure`, which is the whole point of the
  // per-trial override.
  if (state_ == LinkState::Armed || state_ == LinkState::Running) {
    send_error(message_id, "busy", "a trial is armed or running", "timers");
    return;
  }
  uint32_t enable = 0;
  if (!m.u32("enable", &enable)) {
    send_error(message_id, "bad_json", "enable must be a mask", "enable");
    return;
  }
  timers_enabled_ = enable;
  // Applied now, not at the next trial: between trials is exactly when a
  // free-running timer is doing something, and "off" has to mean off.
  owe(pending_ops_, timers_.set_enabled(enable, now_us));

  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::Ack), tx_message_id_);
  w.in_reply_to(message_id);
  // Echoed, so a host never has to infer what took: a mask naming timers the
  // set does not declare is accepted and simply has no effect, and this says
  // what the device is actually holding.
  w.key_u32("enable", enable);
  w.key_u32("n_timers", live_set_.n_timers);
  send(w, message_id);
}

void HostLinkSession::on_pins_request(const JsonObject& m, uint16_t message_id) {
  // docs/reference/protocol.md 3.6. One direction per request, and `dir` is required.
  //
  // Not both in one reply: the labels of a 32-line board do not fit in
  // `max_line`, and a reply that silently held half of them would be worse than
  // no reply at all -- the host would believe it had the whole map. One
  // direction always fits, so this needs no chunking and no partial answer.
  JsonSpan dir;
  if (!m.str("dir", &dir)) {
    send_error(message_id, "bad_json", "no dir", "dir");
    return;
  }
  const bool inputs = json_str_eq(dir, "in");
  if (!inputs && !json_str_eq(dir, "out")) {
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

  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::PinMap), tx_message_id_);
  w.in_reply_to(message_id);
  w.key_str("dir", inputs ? "in" : "out");
  w.key_u32("n", count);
  w.begin_array("pins");
  for (uint8_t i = 0; i < count; ++i) w.elem_str(labels[i] != nullptr ? labels[i] : "");
  w.end_array();
  if (w.overflowed()) {
    // A board with more or longer labels than a line can hold. Refused rather
    // than truncated, for the same reason the whole map is not sent at once.
    send_error(message_id, "too_long", "the pin map does not fit one line", "pins");
    return;
  }
  send(w, message_id);
}

void HostLinkSession::set_wiring(const DeviceWiring& w, bool from_host) {
  wiring_ = w;
  if (from_host) have_wiring_ = true;
  ++wiring_revision_;
}

void HostLinkSession::on_wiring(const JsonObject& m, uint16_t message_id) {
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
  uint32_t v = 0;
  if (m.type_of("invert") != JsonType::Missing) {
    if (!m.u32("invert", &v)) {
      send_error(message_id, "bad_json", "invert must be a mask", "invert");
      return;
    }
    next.inputs.invert_mask = v;
  }
  if (m.type_of("enable") != JsonType::Missing) {
    if (!m.u32("enable", &v)) {
      send_error(message_id, "bad_json", "enable must be a mask", "enable");
      return;
    }
    next.inputs.enable_mask = v;
  }
  if (m.type_of("safe") != JsonType::Missing) {
    if (!m.u32("safe", &v)) {
      send_error(message_id, "bad_json", "safe must be a mask", "safe");
      return;
    }
    next.output_safe_levels = v;
  }
  if (m.type_of("debounce_ms") != JsonType::Missing) {
    JsonArray a;
    if (!m.array("debounce_ms", &a)) {
      send_error(message_id, "bad_json", "debounce_ms must be an array", "debounce_ms");
      return;
    }
    // Every line, not only the ones the array names: a shorter array means the
    // rest are zero, so that removing a debounce is possible at all.
    for (uint8_t i = 0; i < kMaxLines; ++i) next.inputs.debounce_ms[i] = 0;
    for (uint8_t i = 0; i < kMaxLines; ++i) {
      int32_t ms = 0;
      if (!a.next_i32(&ms)) break;
      if (ms < 0 || ms > UINT16_MAX) {
        send_error(message_id, "bad_json", "a debounce is out of range", "debounce_ms");
        return;
      }
      next.inputs.debounce_ms[i] = static_cast<NarrowMilliseconds>(ms);
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

void HostLinkSession::on_upload_message(const JsonObject& m, JsonSpan covered,
                                        uint16_t message_id, Microseconds now_us,
                                        MsgType type) {
  if (state_ == LinkState::Armed || state_ == LinkState::Running) {
    send_error(message_id, "busy", "a trial is armed or running", "graph upload");
    return;
  }

  UploadError e = UploadError::None;
  bool is_end = false;
  switch (type) {
    case MsgType::SetBegin:
      // From here until set_end succeeds the board holds no graph. The builder
      // writes into the live set because two do not fit, so this is where the
      // old double buffering went -- see graph_builder.h.
      have_set_ = false;
      e = builder_.begin_set(m, covered);
      break;
    case MsgType::GraphBegin:
      e = builder_.begin_graph(m, covered);
      break;
    case MsgType::GraphDist:
      e = builder_.add_distribution(m, covered);
      break;
    case MsgType::GraphState:
      e = builder_.add_state(m, covered);
      break;
    case MsgType::GraphTransition:
      e = builder_.add_transition(m, covered);
      break;
    case MsgType::GraphAction:
      e = builder_.add_action(m, covered);
      break;
    case MsgType::GraphTimer:
      e = builder_.add_timer(m, covered);
      break;
    case MsgType::GraphEnd:
      e = builder_.end_graph(m, covered);
      break;
    default:  // set_end; dispatch admits no other type here
      e = builder_.end_set(m);
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

  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::SetOk), tx_message_id_);
  w.in_reply_to(message_id);
  w.key_u32("set_version", live_set_.version);
  w.key_u32("n_graphs", live_set_.n_graphs);
  w.key_u32("n_states", live_set_.n_states);
  w.key_u32("n_transitions", live_set_.n_transitions);
  w.key_u32("n_output_actions", live_set_.n_output_actions);
  send(w, message_id);
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

void HostLinkSession::on_autorun(const JsonObject& m, uint16_t message_id,
                                 Microseconds now_us) {
  // No `enabled` is a question rather than an instruction: report what the
  // board would do, change nothing. Worth having as its own shape because the
  // settings outlive the session that set them, so "what are you configured to
  // do on your own?" is a question a freshly connected daemon has.
  const bool asking = m.type_of("enabled") == JsonType::Missing;

  if (!asking) {
    bool enabled = false;
    if (!m.boolean("enabled", &enabled)) {
      send_error(message_id, "bad_json", "enabled", "enabled");
      return;
    }
    AutorunConfig c = autorun_;
    if (m.type_of("graph_index") != JsonType::Missing && !m.u8("graph_index", &c.graph_index)) {
      send_error(message_id, "bad_json", "graph_index", "graph_index");
      return;
    }
    if (m.type_of("cap_ms") != JsonType::Missing && !m.i32("cap_ms", &c.cap_ms)) {
      send_error(message_id, "bad_json", "cap_ms", "cap_ms");
      return;
    }
    if (m.type_of("seed") != JsonType::Missing && !m.hex64("seed", &c.seed)) {
      send_error(message_id, "bad_json", "seed must be a hex string", "seed");
      return;
    }
    if (m.type_of("first_trial_id") != JsonType::Missing &&
        !m.u32("first_trial_id", &c.first_trial_id)) {
      send_error(message_id, "bad_json", "first_trial_id", "first_trial_id");
      return;
    }

    // Whether to start driving *now*, as opposed to merely recording that this
    // board should. The two are separate because saving requires an idle board:
    // a board already arming its own trials is never idle, so "enable it, then
    // write it down" would be a sequence that could not be performed. With
    // this, a rig is set up while nothing is running -- enable, save, power
    // cycle -- and comes up self-driving from its own storage.
    bool start_now = true;
    if (m.type_of("start_now") != JsonType::Missing && !m.boolean("start_now", &start_now)) {
      send_error(message_id, "bad_json", "start_now", "start_now");
      return;
    }

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

  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::AutorunOk), tx_message_id_);
  w.in_reply_to(message_id);
  w.key_bool("enabled", autorun_.enabled);
  // Not the same fact: the setting is stored and survives a takeover, while
  // `active` is whether this board is driving trials right now. A daemon that
  // greeted a self-driving board sees enabled true and active false, which is
  // exactly what happened.
  w.key_bool("active", autorun_active_);
  w.key_u32("graph_index", autorun_.graph_index);
  w.key_i32("cap_ms", autorun_.cap_ms);
  w.key_u32("next_trial_id", autorun_next_trial_id_);
  send(w, message_id);
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
    JsonWriter unchanged(tx_, sizeof(tx_));
    unchanged.begin(msg_type_name(MsgType::Saved), tx_message_id_);
    unchanged.in_reply_to(message_id);
    unchanged.key_bool("has_set", have_set_);
    unchanged.key_u32("set_version", have_set_ ? live_set_.version : 0);
    unchanged.key_bool("autorun", autorun_.enabled);
    unchanged.key_u32("write_count", settings_write_count_);
    unchanged.key_bool("written", false);
    send(unchanged, message_id);
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

  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::Saved), tx_message_id_);
  w.in_reply_to(message_id);
  w.key_bool("has_set", have_set_);
  w.key_u32("set_version", have_set_ ? live_set_.version : 0);
  w.key_bool("autorun", autorun_.enabled);
  // Flash wear, as a number somebody can see. About 100,000 erase cycles is the
  // budget on the reference board.
  w.key_u32("write_count", settings_write_count_);
  w.key_bool("written", true);
  send(w, message_id);
}

void HostLinkSession::on_configure(const JsonObject& m, uint16_t message_id,
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

  uint32_t trial_id = 0;
  uint16_t version = 0;
  if (!m.u32("trial_id", &trial_id)) {
    send_error(message_id, "bad_json", "no trial_id", "trial_id");
    return;
  }
  if (!m.u16("set_version", &version)) {
    send_error(message_id, "bad_json", "no set_version", "set_version");
    return;
  }
  // A set edit that did not land would otherwise leave the device confidently
  // running the old paradigms.
  if (version != live_set_.version) {
    send_error(message_id, "graph_mismatch", "the device holds a different set", "set_version");
    return;
  }

  // This is the switch. Every graph the session uses is already here, so
  // changing paradigm between two trials costs one field on a message the
  // device was going to receive anyway. See docs/developer/daemon.md 3.2.
  uint8_t graph_index = 0;
  if (m.type_of("graph_index") != JsonType::Missing) {
    if (!m.u8("graph_index", &graph_index)) {
      send_error(message_id, "bad_json", "graph_index", "graph_index");
      return;
    }
  }
  if (graph_index >= live_set_.n_graphs) {
    send_error(message_id, "bad_index", "no graph in that slot", "graph_index");
    return;
  }

  int32_t cap_ms = 0;
  if (m.type_of("cap_ms") != JsonType::Missing && !m.i32("cap_ms", &cap_ms)) {
    send_error(message_id, "bad_json", "cap_ms", "cap_ms");
    return;
  }

  start_from_serial_ = true;
  bool start_from_line = false;
  if (m.type_of("start") != JsonType::Missing) {
    JsonSpan s;
    if (!m.str("start", &s)) {
      send_error(message_id, "bad_json", "start", "start");
      return;
    }
    start_from_serial_ = json_str_eq(s, "serial") || json_str_eq(s, "both");
    start_from_line = json_str_eq(s, "line") || json_str_eq(s, "both");
    if (!start_from_serial_ && !start_from_line) {
      send_error(message_id, "bad_json", "start must be serial, line or both", "start");
      return;
    }
  }

  // Which line, and is it a line that could ever rise. Both refusals are here
  // rather than at the edge that never comes, because a trial armed on a line
  // the board will never see reports nothing at all: it simply waits, and the
  // host has no way to tell that from a subject who has not responded yet.
  uint8_t start_line = 0;
  if (start_from_line) {
    if (!m.u8("start_line", &start_line)) {
      send_error(message_id, "bad_json", "start on a line needs start_line", "start_line");
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
  bool patch_timers = false;
  uint32_t trial_timers = 0;
  if (m.type_of("timers") != JsonType::Missing) {
    if (!m.u32("timers", &trial_timers)) {
      send_error(message_id, "bad_json", "timers must be a mask", "timers");
      return;
    }
    patch_timers = true;
  }

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
  start_from_line_ = start_from_line;
  start_line_ = start_line;
  state_ = LinkState::Armed;

  // Both fields, always: this is the confirmation that start requires, and it
  // is not skippable.
  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::Armed), tx_message_id_);
  w.in_reply_to(message_id);
  w.key_u32("trial_id", trial_id);
  w.key_u32("set_version", live_set_.version);
  w.key_u32("graph_index", graph_index);
  send(w, message_id);
}

void HostLinkSession::on_start(const JsonObject& m, uint16_t message_id, Microseconds now_us) {
  uint32_t trial_id = 0;
  if (!m.u32("trial_id", &trial_id)) {
    send_error(message_id, "bad_json", "no trial_id", "trial_id");
    return;
  }
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

  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::Started), tx_message_id_);
  w.in_reply_to(message_id);
  w.key_u32("trial_id", armed_trial_id_);
  // When the command was accepted, which is not when the trial starts: the run
  // begins on the next scan, together with its pins. A host that needs the
  // trial's own clock reads `entered_us` on the first row of the result, which
  // is the timestamp the machine actually ran on. See the note above.
  w.key_u32("at_us", now_us);
  // Which source started it, so a host reads one field in both cases rather
  // than inferring the answer from whether the message carried an in_reply_to.
  w.key_str("by", "serial");
  send(w, message_id);
}

void HostLinkSession::on_cancel(const JsonObject& m, uint16_t message_id, Microseconds now_us) {
  uint32_t trial_id = 0;
  if (!m.u32("trial_id", &trial_id)) {
    send_error(message_id, "bad_json", "no trial_id", "trial_id");
    return;
  }
  // Refused rather than acked, so the bridge learns that nothing was cancelled.
  if (state_ != LinkState::Running || trial_id != armed_trial_id_) {
    send_error(message_id, "unknown_trial", "no such trial is running", "trial_id");
    return;
  }

  // Reason is checked but only `host` is legal from the host; the others are
  // the device's own account of why it stopped.
  JsonSpan why;
  if (m.type_of("reason") != JsonType::Missing) {
    if (!m.str("reason", &why) || !json_str_eq(why, "host")) {
      send_error(message_id, "bad_json", "only host is a reason the host may give", "reason");
      return;
    }
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

  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::CancelAck), tx_message_id_);
  w.in_reply_to(message_id);
  w.key_u32("trial_id", trial_id);
  w.key_bool("cancelled", cancelled);
  w.key_i32("outcome", static_cast<int32_t>(runner_.result().outcome));
  send(w, message_id);
}

bool HostLinkSession::apply_distribution_patches(const JsonObject& m, uint16_t message_id) {
  if (m.type_of("patch") == JsonType::Missing) return true;

  JsonArray patches;
  if (!m.array("patch", &patches)) {
    send_error(message_id, "bad_json", "patch must be an array", "patch");
    return false;
  }

  JsonSpan element;
  JsonType element_type = JsonType::Missing;
  while (patches.next(&element, &element_type)) {
    if (element_type != JsonType::Object) {
      send_error(message_id, "bad_json", "a patch entry is not an object", "patch");
      revert_distribution_patches();
      return false;
    }
    JsonObject entry(element.p, element.n);
    if (!entry.valid()) {
      send_error(message_id, "bad_json", json_error_str(entry.error()), "patch");
      revert_distribution_patches();
      return false;
    }

    uint8_t index = 0;
    if (!entry.u8("i", &index)) {
      send_error(message_id, "bad_json", "a patch entry has no i", "patch");
      revert_distribution_patches();
      return false;
    }
    if (index >= live_set_.n_distributions) {
      send_error(message_id, "bad_index", "no distribution has that index", "patch");
      revert_distribution_patches();
      return false;
    }
    if (n_patched_distributions_ >= kMaxPatchedDistributions) {
      send_error(message_id, "too_many", "max_patched_distributions", "patch");
      revert_distribution_patches();
      return false;
    }

    // The inverse of the patch, not the patch: what to put back when the trial
    // ends. Saved before anything is written, so a refusal below still reverts
    // cleanly.
    RandomDistribution& distribution = live_set_.distributions[index];
    PatchedDistribution& saved = patched_distributions_[n_patched_distributions_++];
    saved.index = index;
    saved.a = distribution.a;
    saved.b = distribution.b;
    saved.c = distribution.c;

    // Only a, b and c. `kind` may not be patched: that would change the shape
    // of the draw, which is a different graph and a different set_version.
    int32_t value = 0;
    if (entry.type_of("a") != JsonType::Missing) {
      if (!entry.i32("a", &value)) {
        send_error(message_id, "bad_json", "patch a", "patch");
        revert_distribution_patches();
        return false;
      }
      distribution.a = value;
    }
    if (entry.type_of("b") != JsonType::Missing) {
      if (!entry.i32("b", &value)) {
        send_error(message_id, "bad_json", "patch b", "patch");
        revert_distribution_patches();
        return false;
      }
      distribution.b = value;
    }
    if (entry.type_of("c") != JsonType::Missing) {
      if (!entry.i32("c", &value)) {
        send_error(message_id, "bad_json", "patch c", "patch");
        revert_distribution_patches();
        return false;
      }
      distribution.c = value;
    }
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
  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::Pong), tx_message_id_);
  w.in_reply_to(message_id);
  w.key_u32("up_us", since(booted_us_, now_us));
  // The device clock itself, raw, wrapping every ~71 minutes. `up_us` counts
  // from the first time anything asked, which is a different origin on every
  // session and cannot be compared with the `entered_us` in a result. This is
  // the value a host correlates against its own clock -- see docs/developer/daemon.md 4.5,
  // where that correlation is called load-bearing.
  w.key_u32("us", now_us);
  send(w, message_id);
}

void HostLinkSession::on_state_request(uint16_t message_id, Microseconds now_us) {
  if (!have_boot_) {
    booted_us_ = now_us;
    have_boot_ = true;
  }
  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::StateReport), tx_message_id_);
  w.in_reply_to(message_id);
  w.key_u32("link_state", static_cast<uint32_t>(state_));
  // Nested for the reason `io` and `scan` are, and it is a hard limit rather
  // than a preference: a message is capped at kJsonMaxMembers, and that cap is
  // what bounds JsonObject's stack footprint. Four more top-level members here
  // would push state_report past it and make the reply unparsable.
  w.begin_object("graph");
  w.key_bool("has_set", have_set_);
  w.key_u32("set_version", have_set_ ? live_set_.version : 0);
  w.key_u32("n_graphs", have_set_ ? live_set_.n_graphs : 0);
  w.key_u32("index", runner_.graph_index());
  w.end_object();
  w.key_bool("has_wiring", have_wiring_);
  // Whether this board is arming its own trials. `link_state` already says
  // Relighting during the dwell between two of them, but not while one is in
  // flight -- and "who started this trial" is exactly what a daemon that has
  // just connected to a rig needs to know. This takes state_report to
  // kJsonMaxMembers exactly: another top-level member here is a message the
  // device's own parser would refuse, so anything further nests.
  w.key_bool("autorun", autorun_active_);
  w.key_u32("trial_id", armed_trial_id_);
  // `running` is the answer to "did the host's start take", not "has the engine
  // ticked yet": between `started` going out and the scan that begins the run
  // there is up to one scan period in which the runner has not started and the
  // trial unarguably has. Reporting false there would make a state_report sent
  // straight after a start contradict the `started` it just received.
  w.key_bool("running", start_pending_ || runner_.running());
  w.key_u32("current_state",
            start_pending_ ? entry_state_of_live_graph() : runner_.current_state());
  w.key_u32("up_us", since(booted_us_, now_us));
  // Diagnosis, not control: a link dropping lines should be visible to whoever
  // is debugging the rig rather than inferred from trials that did not happen.
  w.key_u32("dropped_lines", reader_.dropped());
  w.key_u32("bad_lines", bad_lines_);
  // Nested rather than three more top-level members: a message is capped at
  // kJsonMaxMembers, and that cap is a RAM decision about JsonObject's stack
  // footprint rather than a formatting preference.
  // The live pins, nested for the same reason `scan` is -- a message is capped
  // at kJsonMaxMembers, and that cap bounds JsonObject's stack footprint.
  //
  // This is the only way anything outside the device can check that a graph's
  // line numbers land on the pins somebody actually wired: there is no read-back
  // path from a pin, and `out` is the engine's own shadow rather than a
  // measurement. Under emulation it is what makes the pin map testable at all.
  w.begin_object("io");
  w.key_u32("in", last_word_);
  w.key_u32("out", runner_.driven_levels());
  w.end_object();
  w.begin_object("scan");
  w.key_u32("hz", scan_.hz);
  w.key_u32("overruns", scan_.overruns);
  w.key_u32("worst_gap", scan_.worst_gap);
  w.key_u32("tx_stalls", scan_.tx_stalls);
  // Same question as the counters above it -- is this device keeping up -- and
  // nested here because state_report is at kJsonMaxMembers exactly.
  w.key_u32("visits_dropped", dropped_visits_);
  // The timers, in here for the same reason and not because they are a scan
  // health statistic: the top level of state_report is full, and this is the
  // object that already holds what the scan is doing. `enabled` is the mask in
  // force *now*, so during a trial that overrode it this is the trial's, not
  // the device's. `running` is one bit per timer actually in flight, delay
  // included -- a timer counting down its onset is running, it is simply not
  // high yet.
  w.key_u32("timers_enabled", timers_.enabled());
  w.key_u32("timers_running", timers_.bits() >> kFirstTimerLine);
  w.end_object();
  send(w, message_id);
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
    // on a board is the timer ISR. ReplySink::send_line() spins when the
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
  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::Started), tx_message_id_);
  // No in_reply_to: nothing asked. This is the device reporting an event, in
  // the same class as `visit` and `result`.
  w.key_u32("trial_id", armed_trial_id_);
  // Unlike the serial path's, this `at_us` *is* the start of the trial and not
  // an acknowledgement of a command: it is the timestamp of the scan that saw
  // the edge, which is the timestamp that scan stamped `entered_us` with. See
  // on_start() for why the two cases differ.
  w.key_u32("at_us", line_started_at_us_);
  w.key_str("by", "line");
  w.key_u32("line", start_line_);
  send_unsolicited(w);
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
    JsonWriter w(tx_, sizeof(tx_));
    w.begin(msg_type_name(MsgType::Visit), tx_message_id_);
    w.key_u32("trial_id", p.trial_id);
    w.key_u32("seq", p.seq);
    // The same six-element array as a result_path entry, decoded by the same
    // function on the host. Two shapes for one fact is how the two drift apart.
    w.begin_array("v");
    w.elem_u32(p.visit.state_index);
    w.elem_str(cause_name(p.visit.cause));
    w.elem_u32(p.visit.transition_index);
    w.elem_i32(p.visit.drawn_ms);
    w.elem_u32(p.visit.entered_us);
    w.elem_u32(p.visit.duration_us);
    w.end_array();
    send_unsolicited(w);
    // Last, so the producer never sees a slot freed before it has been read.
    visit_tail_ = static_cast<uint8_t>((visit_tail_ + 1) % kVisitRingDepth);
  }
}

void HostLinkSession::emit_result() {
  const TrialRecord& r = runner_.result();
  const StateMachineRunRecord& run = runner_.run_record();

  uint16_t checksum = 0xFFFF;

  {
    JsonWriter w(tx_, sizeof(tx_));
    w.begin(msg_type_name(MsgType::ResultBegin), tx_message_id_);
    w.key_u32("trial_id", r.trial_id);
    w.key_i32("outcome", static_cast<int32_t>(r.outcome));
    w.key_u32("cancel_reason", static_cast<uint32_t>(r.cancel_reason));
    w.key_u32("total_us", run.total_us);
    w.key_u32("path_len", run.path_len);
    // What the host needs to know which window it received. `truncated` says a
    // path overflowed; these say by how much and where the surviving one
    // starts, so a trace assembled from the visit stream can be reconciled
    // against it rather than merely compared for length.
    w.key_u32("first_seq", run.first_seq());
    w.key_u32("total_visits", run.total_visits);
    w.key_bool("truncated", run.path_truncated);
    const size_t len = w.len();
    checksum = crc16_ccitt(tx_, len, checksum);
    send_unsolicited(w);
  }

  // Chunked for the same reason the upload is: a full path does not fit in one
  // line, and buffering one that did would cost a kilobyte this board does not
  // have. Rows are packed until the next one would not fit rather than in a
  // fixed batch, so the line size is the only thing that decides the count.
  uint8_t from = 0;
  while (from < run.path_len) {
    JsonWriter w(tx_, sizeof(tx_));
    w.begin(msg_type_name(MsgType::ResultPath), tx_message_id_);
    w.key_u32("trial_id", r.trial_id);
    w.key_u32("from", from);
    w.begin_array("p");

    // A row is six numbers plus a short word; 64 bytes is comfortably above the
    // worst case and cheaper than measuring it twice.
    constexpr size_t kRowBudget = 64;
    constexpr size_t kTailBudget = 16;  // ],"crc":"XXXX"}\n
    uint8_t i = from;
    while (i < run.path_len && w.len() + kRowBudget + kTailBudget < sizeof(tx_)) {
      // visit(), not path[i]: the ring drops from the front, so slot 0 is not
      // visit 0 once it has wrapped. `from` stays an offset into what was sent.
      const StateVisit& v = run.visit(i);
      w.begin_elem_array();
      w.elem_u32(v.state_index);
      w.elem_str(cause_name(v.cause));
      w.elem_u32(v.transition_index);
      w.elem_i32(v.drawn_ms);
      w.elem_u32(v.entered_us);
      w.elem_u32(v.duration_us);
      w.end_array();
      ++i;
    }
    w.end_array();
    const size_t len = w.len();
    checksum = crc16_ccitt(tx_, len, checksum);
    send_unsolicited(w);

    if (i == from) break;  // a row that cannot fit at all; refuse to spin
    from = i;
  }

  {
    char hex[4];
    crc16_to_hex(checksum, hex);
    char text[5] = {hex[0], hex[1], hex[2], hex[3], '\0'};
    JsonWriter w(tx_, sizeof(tx_));
    w.begin(msg_type_name(MsgType::ResultEnd), tx_message_id_);
    w.key_u32("trial_id", r.trial_id);
    // Catches a dropped chunk, which no per-line crc can see: the line that
    // vanished was perfectly well formed.
    w.key_str("checksum", text);
    send_unsolicited(w);
  }
}

// --------------------------------------------------------------- sending ---

namespace {

/// The body every refusal shares, whether or not it can name a `message_id`.
void write_error_body(JsonWriter& w, const char* code, const char* message,
                      const char* context) {
  w.key_str("code", code);
  w.key_str("message", message);
  // Every refusal names what to change; an empty context is a defect here
  // rather than a terse style.
  w.key_str("context", (context != nullptr && context[0] != '\0') ? context : code);
}

}  // namespace

void HostLinkSession::send_error(uint16_t message_id, const char* code, const char* message,
                                 const char* context) {
  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::Error), tx_message_id_);
  w.in_reply_to(message_id);
  write_error_body(w, code, message, context);
  send(w, message_id);
}

// Split from send_error rather than folded into it behind a sentinel value.
// Zero is a perfectly ordinary message_id -- the counter is a u16 that wraps through
// it, and the bridge's first command of a session is usually numbered 0 -- so
// a `message_id != 0` test here answered a real command with a reply carrying no
// `in_reply_to` and remembered nothing for the retry guard, which is precisely
// command a bridge would resend and precisely the resend that must not
// re-execute. Whether a message_id was read is a fact about the line, and the only
// thing that can know it is the caller.
void HostLinkSession::send_orphan_error(const char* code, const char* message,
                                        const char* context) {
  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::Error), tx_message_id_);
  write_error_body(w, code, message, context);
  send_unsolicited(w);
}

void HostLinkSession::send_ack(uint16_t message_id) {
  JsonWriter w(tx_, sizeof(tx_));
  w.begin(msg_type_name(MsgType::Ack), tx_message_id_);
  w.in_reply_to(message_id);
  send(w, message_id);
}

void HostLinkSession::send(JsonWriter& w, uint16_t message_id) {
  const size_t n = w.finish();
  if (n == 0) return;  // a message that does not fit is a firmware bug, not a wire condition
  ++tx_message_id_;
  guard_.remember(message_id, tx_, n);
  out_.send_line(tx_, n);
}

void HostLinkSession::send_unsolicited(JsonWriter& w) {
  const size_t n = w.finish();
  if (n == 0) return;
  ++tx_message_id_;
  out_.send_line(tx_, n);
}

}  // namespace statemachined
