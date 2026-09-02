#include "protocol/host_link_session.h"

#include "protocol/crc16.h"

namespace fsmd {
namespace {

constexpr uint16_t kProtocolVersion = 1;

/// Microseconds between two device-clock readings, correct across the ~71
/// minute wrap because unsigned subtraction is.
Microseconds since(Microseconds from, Microseconds to) { return to - from; }

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

/// The protocol error code for an upload failure. dev/PROTOCOL.md 5 -- the
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

void DuplicateCommandGuard::remember(uint16_t seq, const char* line, size_t n) {
  if (n > sizeof(reply_)) {  // cannot happen: everything is built in a kMaxLine buffer
    have_ = false;
    return;
  }
  for (size_t i = 0; i < n; ++i) reply_[i] = line[i];
  reply_len_ = n;
  last_seq_ = seq;
  have_ = true;
}

HostLinkSession::HostLinkSession(ReplySink& out, const DeviceIdentity& identity)
    : out_(out), identity_(identity), reader_(rx_, sizeof(rx_)), runner_(live_graph_) {}

// ------------------------------------------------------------- receiving ---

void HostLinkSession::receive(const char* bytes, size_t n, Microseconds now_us) {
  for (size_t i = 0; i < n; ++i) receive_byte(bytes[i], now_us);
}

void HostLinkSession::receive_byte(char c, Microseconds now_us) {
  if (!reader_.feed(c)) return;

  if (reader_.status() != FrameError::None) {
    // The line never assembled, so there is no seq to attribute the error to.
    // Reported anyway rather than dropped: a host waiting for an answer it will
    // never get is worse than one told its line was unusable.
    ++bad_lines_;
    const char* code = reader_.status() == FrameError::TooLong ? "too_long" : "bad_json";
    send_error(0, code, frame_error_str(reader_.status()), "line");
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
    send_error(0, code, frame_error_str(fe), "line");
    return;
  }

  JsonObject m(line, n);
  if (!m.valid()) {
    ++bad_lines_;
    send_error(0, "bad_json", json_error_str(m.error()), "line");
    return;
  }

  uint16_t seq = 0;
  if (!m.u16("seq", &seq)) {
    send_error(0, "bad_json", "no seq", "seq");
    return;
  }

  // A retry of the command just answered gets that answer back verbatim and
  // changes nothing. Acting twice is what matters: a re-executed start would
  // run a second trial.
  if (guard_.is_repeat(seq)) {
    out_.send_line(guard_.reply(), guard_.reply_len());
    return;
  }

  dispatch(m, JsonSpan{f.covered, f.covered_len}, seq, now_us);
}

void HostLinkSession::dispatch(const JsonObject& m, JsonSpan covered, uint16_t seq,
                               Microseconds now_us) {
  JsonSpan t;
  if (!m.str("t", &t)) {
    send_error(seq, "bad_json", "no message type", "t");
    return;
  }

  // hello is the only thing accepted before a hello: everything else needs a
  // session seed, and a device answering commands without one would be running
  // trials nobody could replay.
  if (state_ == LinkState::Greeting && !json_str_eq(t, "hello")) {
    send_error(seq, "not_ready", "no hello yet", "hello");
    return;
  }

  if (json_str_eq(t, "hello")) return on_hello(m, seq);
  if (json_str_eq(t, "ping")) return on_ping(seq, now_us);
  if (json_str_eq(t, "state")) return on_state_request(seq, now_us);
  if (json_str_eq(t, "configure")) return on_configure(m, seq);
  if (json_str_eq(t, "start")) return on_start(m, seq, now_us);
  if (json_str_eq(t, "cancel")) return on_cancel(m, seq, now_us);

  if (json_str_eq(t, "graph_begin") || json_str_eq(t, "graph_dist") ||
      json_str_eq(t, "graph_state") || json_str_eq(t, "graph_transition") ||
      json_str_eq(t, "graph_action") || json_str_eq(t, "graph_end"))
    return on_graph_message(m, covered, seq, t);

  send_error(seq, "unknown_type", "unrecognised message type", "t");
}

// -------------------------------------------------------------- handlers ---

void HostLinkSession::on_hello(const JsonObject& m, uint16_t seq) {
  uint16_t proto = 0;
  if (!m.u16("proto", &proto)) {
    send_error(seq, "bad_json", "no proto", "proto");
    return;
  }
  if (proto != kProtocolVersion) {
    send_error(seq, "bad_proto", "this firmware does not speak that version", "proto");
    return;
  }
  uint64_t seed = 0;
  if (!m.hex64("seed", &seed)) {
    send_error(seq, "bad_json", "seed must be a hex string", "seed");
    return;
  }

  // Resets to idle and abandons any half-finished upload, but deliberately does
  // NOT clear the committed graph: reconnecting the bridge must not cost a
  // re-upload.
  session_seed_ = seed;
  builder_.abandon();
  reader_.reset();
  state_ = LinkState::Idle;
  armed_trial_id_ = 0;

  JsonWriter w(tx_, sizeof(tx_));
  w.begin("hello_ack", tx_seq_);
  w.req(seq);
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
  w.end_object();
  w.key_bool("has_graph", have_graph_);
  w.key_u32("graph_version", have_graph_ ? live_graph_.version : 0);
  send(w, seq);
}

void HostLinkSession::on_graph_message(const JsonObject& m, JsonSpan covered, uint16_t seq,
                                       JsonSpan type) {
  if (state_ == LinkState::Armed || state_ == LinkState::Running) {
    send_error(seq, "busy", "a trial is armed or running", "graph upload");
    return;
  }

  UploadError e = UploadError::None;
  bool is_end = false;
  if (json_str_eq(type, "graph_begin")) {
    e = builder_.begin(m, covered);
  } else if (json_str_eq(type, "graph_dist")) {
    e = builder_.add_distribution(m, covered);
  } else if (json_str_eq(type, "graph_state")) {
    e = builder_.add_state(m, covered);
  } else if (json_str_eq(type, "graph_transition")) {
    e = builder_.add_transition(m, covered);
  } else if (json_str_eq(type, "graph_action")) {
    e = builder_.add_action(m, covered);
  } else {
    e = builder_.end(m);
    is_end = true;
  }

  if (e != UploadError::None) {
    send_error(seq, upload_error_code(e), upload_error_str(e), builder_.context());
    return;
  }

  if (!is_end) {
    send_ack(seq);
    return;
  }

  // The live graph is untouched until here, so a failed or abandoned upload
  // leaves the device running the paradigm it was already running. Copying is
  // what makes the swap atomic from the caller's point of view, and StateGraph's
  // copy re-points the choice pools so the new graph refers to its own.
  live_graph_ = builder_.staged();
  have_graph_ = true;
  runner_ = TrialRunner(live_graph_);

  JsonWriter w(tx_, sizeof(tx_));
  w.begin("graph_ok", tx_seq_);
  w.req(seq);
  w.key_u32("graph_version", live_graph_.version);
  w.key_u32("n_states", live_graph_.n_states);
  w.key_u32("n_transitions", live_graph_.n_transitions);
  w.key_u32("n_output_actions", live_graph_.n_output_actions);
  send(w, seq);
}

void HostLinkSession::on_configure(const JsonObject& m, uint16_t seq) {
  if (!have_graph_) {
    send_error(seq, "not_ready", "no graph has been committed", "graph");
    return;
  }
  if (state_ == LinkState::Running) {
    send_error(seq, "busy", "a trial is already running", "trial");
    return;
  }

  uint32_t trial_id = 0;
  uint16_t version = 0;
  if (!m.u32("trial_id", &trial_id)) {
    send_error(seq, "bad_json", "no trial_id", "trial_id");
    return;
  }
  if (!m.u16("graph_version", &version)) {
    send_error(seq, "bad_json", "no graph_version", "graph_version");
    return;
  }
  // A graph edit that did not land would otherwise leave the device confidently
  // running the old paradigm.
  if (version != live_graph_.version) {
    send_error(seq, "graph_mismatch", "the device holds a different graph", "graph_version");
    return;
  }

  int32_t cap_ms = 0;
  if (m.type_of("cap_ms") != JsonType::Missing && !m.i32("cap_ms", &cap_ms)) {
    send_error(seq, "bad_json", "cap_ms", "cap_ms");
    return;
  }

  start_from_serial_ = true;
  if (m.type_of("start") != JsonType::Missing) {
    JsonSpan s;
    if (!m.str("start", &s)) {
      send_error(seq, "bad_json", "start", "start");
      return;
    }
    start_from_serial_ = json_str_eq(s, "serial") || json_str_eq(s, "both");
    if (!start_from_serial_ && !json_str_eq(s, "line")) {
      send_error(seq, "bad_json", "start must be serial, line or both", "start");
      return;
    }
  }

  runner_ = TrialRunner(live_graph_);
  runner_.set_trial_cap_ms(cap_ms);
  armed_trial_id_ = trial_id;
  state_ = LinkState::Armed;

  // Both fields, always: this is the confirmation that start requires, and it
  // is not skippable.
  JsonWriter w(tx_, sizeof(tx_));
  w.begin("armed", tx_seq_);
  w.req(seq);
  w.key_u32("trial_id", trial_id);
  w.key_u32("graph_version", live_graph_.version);
  send(w, seq);
}

void HostLinkSession::on_start(const JsonObject& m, uint16_t seq, Microseconds now_us) {
  uint32_t trial_id = 0;
  if (!m.u32("trial_id", &trial_id)) {
    send_error(seq, "bad_json", "no trial_id", "trial_id");
    return;
  }
  // No trial runs that the device was not confirmed configured for.
  if (state_ != LinkState::Armed) {
    send_error(seq, "not_ready", "not armed", "configure first");
    return;
  }
  if (trial_id != armed_trial_id_) {
    send_error(seq, "unknown_trial", "armed for a different trial", "trial_id");
    return;
  }
  if (!start_from_serial_) {
    send_error(seq, "not_ready", "this trial starts on a line, not on serial", "start");
    return;
  }

  runner_.start(armed_trial_id_, session_seed_, now_us);
  state_ = LinkState::Running;

  JsonWriter w(tx_, sizeof(tx_));
  w.begin("started", tx_seq_);
  w.req(seq);
  w.key_u32("trial_id", armed_trial_id_);
  w.key_u32("at_us", now_us);
  send(w, seq);
}

void HostLinkSession::on_cancel(const JsonObject& m, uint16_t seq, Microseconds now_us) {
  uint32_t trial_id = 0;
  if (!m.u32("trial_id", &trial_id)) {
    send_error(seq, "bad_json", "no trial_id", "trial_id");
    return;
  }
  // Refused rather than acked, so the bridge learns that nothing was cancelled.
  if (state_ != LinkState::Running || trial_id != armed_trial_id_) {
    send_error(seq, "unknown_trial", "no such trial is running", "trial_id");
    return;
  }

  // Reason is checked but only `host` is legal from the host; the others are
  // the device's own account of why it stopped.
  JsonSpan why;
  if (m.type_of("reason") != JsonType::Missing) {
    if (!m.str("reason", &why) || !json_str_eq(why, "host")) {
      send_error(seq, "bad_json", "only host is a reason the host may give", "reason");
      return;
    }
  }

  // Loses the race against a graph that already reached a terminal state. The
  // bridge must cope with asking to cancel and being told Hit -- the
  // alternative is a record claiming a trial was cancelled when the animal had
  // already responded.
  const bool cancelled = runner_.cancel(TrialCancelReason::Host, now_us);

  JsonWriter w(tx_, sizeof(tx_));
  w.begin("cancel_ack", tx_seq_);
  w.req(seq);
  w.key_u32("trial_id", trial_id);
  w.key_bool("cancelled", cancelled);
  w.key_i32("outcome", static_cast<int32_t>(runner_.result().outcome));
  send(w, seq);
}

void HostLinkSession::on_ping(uint16_t seq, Microseconds now_us) {
  if (!have_boot_) {
    booted_us_ = now_us;
    have_boot_ = true;
  }
  JsonWriter w(tx_, sizeof(tx_));
  w.begin("pong", tx_seq_);
  w.req(seq);
  w.key_u32("up_us", since(booted_us_, now_us));
  send(w, seq);
}

void HostLinkSession::on_state_request(uint16_t seq, Microseconds now_us) {
  if (!have_boot_) {
    booted_us_ = now_us;
    have_boot_ = true;
  }
  JsonWriter w(tx_, sizeof(tx_));
  w.begin("state_report", tx_seq_);
  w.req(seq);
  w.key_u32("link_state", static_cast<uint32_t>(state_));
  w.key_bool("has_graph", have_graph_);
  w.key_u32("graph_version", have_graph_ ? live_graph_.version : 0);
  w.key_u32("trial_id", armed_trial_id_);
  w.key_bool("running", runner_.running());
  w.key_u32("current_state", runner_.current_state());
  w.key_u32("up_us", since(booted_us_, now_us));
  // Diagnosis, not control: a link dropping lines should be visible to whoever
  // is debugging the rig rather than inferred from trials that did not happen.
  w.key_u32("dropped_lines", reader_.dropped());
  w.key_u32("bad_lines", bad_lines_);
  // Nested rather than three more top-level members: a message is capped at
  // kJsonMaxMembers, and that cap is a RAM decision about JsonObject's stack
  // footprint rather than a formatting preference.
  w.begin_object("scan");
  w.key_u32("hz", scan_.hz);
  w.key_u32("overruns", scan_.overruns);
  w.key_u32("worst_gap", scan_.worst_gap);
  w.end_object();
  send(w, seq);
}

// ------------------------------------------------------------ trial loop ---

OutputUpdate HostLinkSession::advance_trial(LineBitmask word, Microseconds now_us) {
  if (!have_boot_) {
    booted_us_ = now_us;
    have_boot_ = true;
  }
  // A pulse raised by the last act of a trial -- the ordinary way to write a
  // reward -- falls due after the run has ended and the session is back to
  // Idle. Returning an empty update here would leave the valve open until the
  // next trial started.
  if (state_ != LinkState::Running) return runner_.service_outputs(now_us);

  const OutputUpdate ops = runner_.advance(word, now_us);
  if (!runner_.running()) {
    // The host learns of an outcome without having to ask for it. A trial that
    // ended silently would be indistinguishable from a hung one.
    emit_result();
    state_ = LinkState::Idle;
  }
  return ops;
}

OutputUpdate HostLinkSession::link_lost(Microseconds now_us) {
  OutputUpdate ops;
  if (state_ == LinkState::Running) {
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
  armed_trial_id_ = 0;
  builder_.abandon();
  reader_.reset();
  guard_.forget();
  return ops;
}

OutputUpdate HostLinkSession::fail_safe() {
  OutputUpdate ops;
  const LineBitmask safe = have_graph_ ? live_graph_.output_safe_levels : 0;
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

void HostLinkSession::emit_result() {
  const TrialRecord& r = runner_.result();
  const StateMachineRunRecord& run = runner_.run_record();

  uint16_t checksum = 0xFFFF;

  {
    JsonWriter w(tx_, sizeof(tx_));
    w.begin("result_begin", tx_seq_);
    w.key_u32("trial_id", r.trial_id);
    w.key_i32("outcome", static_cast<int32_t>(r.outcome));
    w.key_u32("cancel_reason", static_cast<uint32_t>(r.cancel_reason));
    w.key_u32("total_us", run.total_us);
    w.key_u32("path_len", run.path_len);
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
    w.begin("result_path", tx_seq_);
    w.key_u32("trial_id", r.trial_id);
    w.key_u32("from", from);
    w.begin_array("p");

    // A row is six numbers plus a short word; 64 bytes is comfortably above the
    // worst case and cheaper than measuring it twice.
    constexpr size_t kRowBudget = 64;
    constexpr size_t kTailBudget = 16;  // ],"crc":"XXXX"}\n
    uint8_t i = from;
    while (i < run.path_len && w.len() + kRowBudget + kTailBudget < sizeof(tx_)) {
      const StateVisit& v = run.path[i];
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
    w.begin("result_end", tx_seq_);
    w.key_u32("trial_id", r.trial_id);
    // Catches a dropped chunk, which no per-line crc can see: the line that
    // vanished was perfectly well formed.
    w.key_str("checksum", text);
    send_unsolicited(w);
  }
}

// --------------------------------------------------------------- sending ---

void HostLinkSession::send_error(uint16_t seq, const char* code, const char* message,
                                 const char* context) {
  JsonWriter w(tx_, sizeof(tx_));
  w.begin("error", tx_seq_);
  if (seq != 0) w.req(seq);
  w.key_str("code", code);
  w.key_str("message", message);
  // Every refusal names what to change; an empty context is a defect here
  // rather than a terse style.
  w.key_str("context", (context != nullptr && context[0] != '\0') ? context : code);
  if (seq != 0) {
    send(w, seq);
  } else {
    send_unsolicited(w);
  }
}

void HostLinkSession::send_ack(uint16_t seq) {
  JsonWriter w(tx_, sizeof(tx_));
  w.begin("ack", tx_seq_);
  w.req(seq);
  send(w, seq);
}

void HostLinkSession::send(JsonWriter& w, uint16_t seq) {
  const size_t n = w.finish();
  if (n == 0) return;  // a message that does not fit is a firmware bug, not a wire condition
  ++tx_seq_;
  guard_.remember(seq, tx_, n);
  out_.send_line(tx_, n);
}

void HostLinkSession::send_unsolicited(JsonWriter& w) {
  const size_t n = w.finish();
  if (n == 0) return;
  ++tx_seq_;
  out_.send_line(tx_, n);
}

}  // namespace fsmd
