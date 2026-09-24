// SPDX-License-Identifier: GPL-3.0-or-later
#include "link_json.h"

#include <pb_decode.h>
#include <pb_encode.h>

#include <cstring>

#include "protocol/framing.h"
#include "wire_json.h"

namespace statemachined::test {
namespace {

/// A token on the old wire, and the enum value that carries it now.
struct Token {
  const char* text;
  int value;
};

/// A value no enum in link.proto declares. proto3 enums are open, so it
/// crosses the wire as it is -- which is how a test sends a token the board
/// has never heard of, and checks that it is refused rather than guessed at.
constexpr int kUnknownToken = 99;

class Reader {
 public:
  Reader(const JsonObject& m, std::string* why) : m_(m), why_(why) {}

  bool ok() const { return ok_; }

  bool has(const char* key) const {
    const JsonType t = m_.type_of(key);
    return t != JsonType::Missing && t != JsonType::Null;
  }

  template <typename T>
  void u32(const char* key, T* out) {
    if (!has(key)) return;
    uint32_t v = 0;
    if (!m_.u32(key, &v)) return fail(key, "not an unsigned number");
    *out = static_cast<T>(v);
  }

  /// An `optional` field: set, and its has_ flag with it, only when present
  /// and not null.
  template <typename T>
  void u32(const char* key, bool* has_out, T* out) {
    if (!has(key)) return;
    *has_out = true;
    u32(key, out);
  }

  void i32(const char* key, int32_t* out) {
    if (!has(key)) return;
    if (!m_.i32(key, out)) fail(key, "not a signed number");
  }

  void i32(const char* key, bool* has_out, int32_t* out) {
    if (!has(key)) return;
    *has_out = true;
    i32(key, out);
  }

  void boolean(const char* key, bool* out) {
    if (!has(key)) return;
    if (!m_.boolean(key, out)) fail(key, "not a bool");
  }

  void boolean(const char* key, bool* has_out, bool* out) {
    if (!has(key)) return;
    *has_out = true;
    boolean(key, out);
  }

  /// A seed or a checksum: the hex string the NDJSON wire needed for them, or
  /// a plain number.
  void hex(const char* key, uint64_t* out) {
    if (!has(key)) return;
    if (m_.type_of(key) == JsonType::String) {
      JsonSpan s;
      m_.str(key, &s);
      uint64_t v = 0;
      for (size_t i = 0; i < s.n; ++i) {
        const char c = s.p[i];
        int d = -1;
        if (c >= '0' && c <= '9') d = c - '0';
        if (c >= 'A' && c <= 'F') d = c - 'A' + 10;
        if (c >= 'a' && c <= 'f') d = c - 'a' + 10;
        if (d < 0) return fail(key, "not hex");
        v = (v << 4) | static_cast<uint64_t>(d);
      }
      *out = v;
      return;
    }
    uint32_t v = 0;
    if (!m_.u32(key, &v)) return fail(key, "not hex or a number");
    *out = v;
  }

  template <typename E, size_t N>
  void token(const char* key, E* out, const Token (&tokens)[N]) {
    if (!has(key)) return;
    JsonSpan s;
    if (!m_.str(key, &s)) return fail(key, "not a string");
    for (const Token& t : tokens) {
      if (json_str_eq(s, t.text)) {
        *out = static_cast<E>(t.value);
        return;
      }
    }
    *out = static_cast<E>(kUnknownToken);
  }

  template <typename T, size_t N>
  void u32_list(const char* key, pb_size_t* count, T (&out)[N]) {
    if (!has(key)) return;
    JsonArray a;
    if (!m_.array(key, &a)) return fail(key, "not an array");
    uint32_t v = 0;
    *count = 0;
    while (a.next_u32(&v)) {
      if (*count == N) return fail(key, "longer than link.options allows");
      out[(*count)++] = static_cast<T>(v);
    }
  }

  template <size_t N>
  void i32_list(const char* key, pb_size_t* count, int32_t (&out)[N]) {
    if (!has(key)) return;
    JsonArray a;
    if (!m_.array(key, &a)) return fail(key, "not an array");
    int32_t v = 0;
    *count = 0;
    while (a.next_i32(&v)) {
      if (*count == N) return fail(key, "longer than link.options allows");
      out[(*count)++] = v;
    }
  }

  bool object(const char* key, JsonObject* out) {
    if (!has(key)) return false;
    if (!m_.object(key, out)) {
      fail(key, "not an object");
      return false;
    }
    return true;
  }

  bool array(const char* key, JsonArray* out) {
    if (!has(key)) return false;
    if (!m_.array(key, out)) {
      fail(key, "not an array");
      return false;
    }
    return true;
  }

  void fail(const char* key, const char* what) {
    if (!ok_) return;
    ok_ = false;
    *why_ = std::string(key) + ": " + what;
  }

 private:
  const JsonObject& m_;
  std::string* why_;
  bool ok_ = true;
};

constexpr Token kDistKinds[] = {
    {"fixed", statemachined_link_v1_DistKind_DIST_KIND_FIXED},
    {"uniform", statemachined_link_v1_DistKind_DIST_KIND_UNIFORM},
    {"exponential", statemachined_link_v1_DistKind_DIST_KIND_EXPONENTIAL},
    {"choice", statemachined_link_v1_DistKind_DIST_KIND_CHOICE},
};
constexpr Token kActionOn[] = {
    {"entry", statemachined_link_v1_ActionOn_ACTION_ON_ENTRY},
    {"exit", statemachined_link_v1_ActionOn_ACTION_ON_EXIT},
};
constexpr Token kActionKinds[] = {
    {"high", statemachined_link_v1_ActionKind_ACTION_KIND_HIGH},
    {"low", statemachined_link_v1_ActionKind_ACTION_KIND_LOW},
    {"toggle", statemachined_link_v1_ActionKind_ACTION_KIND_TOGGLE},
    {"pulse", statemachined_link_v1_ActionKind_ACTION_KIND_PULSE},
    {"timer_start", statemachined_link_v1_ActionKind_ACTION_KIND_TIMER_START},
    {"timer_cancel", statemachined_link_v1_ActionKind_ACTION_KIND_TIMER_CANCEL},
};
constexpr Token kStartSources[] = {
    {"serial", statemachined_link_v1_StartSource_START_SOURCE_SERIAL},
    {"line", statemachined_link_v1_StartSource_START_SOURCE_LINE},
    {"both", statemachined_link_v1_StartSource_START_SOURCE_BOTH},
};
constexpr Token kCancelReasons[] = {
    {"host", statemachined_link_v1_CancelReason_CANCEL_REASON_HOST},
    {"link_lost", statemachined_link_v1_CancelReason_CANCEL_REASON_LINK_LOST},
    {"abort_line", statemachined_link_v1_CancelReason_CANCEL_REASON_ABORT_LINE},
    {"trial_timeout", statemachined_link_v1_CancelReason_CANCEL_REASON_TRIAL_TIMEOUT},
};
constexpr Token kDirections[] = {
    {"in", statemachined_link_v1_Direction_DIRECTION_IN},
    {"out", statemachined_link_v1_Direction_DIRECTION_OUT},
};

const char* token_of(int value, const Token* tokens, size_t n) {
  for (size_t i = 0; i < n; ++i)
    if (tokens[i].value == value) return tokens[i].text;
  return "?";
}

template <size_t N>
const char* token_of(int value, const Token (&tokens)[N]) {
  return token_of(value, tokens, N);
}

const char* exit_token(statemachined_link_v1_ExitCause c) {
  switch (c) {
    case statemachined_link_v1_ExitCause_EXIT_CAUSE_TIMEOUT:
      return "timeout";
    case statemachined_link_v1_ExitCause_EXIT_CAUSE_TRANSITION:
      return "transition";
    case statemachined_link_v1_ExitCause_EXIT_CAUSE_CANCEL:
      return "cancel";
    case statemachined_link_v1_ExitCause_EXIT_CAUSE_TERMINAL:
      return "terminal";
    default:
      return "?";
  }
}

}  // namespace

bool host_message_from_json(const std::string& text, link::HostMessage* out, std::string* why) {
  *out = statemachined_link_v1_HostMessage_init_zero;
  JsonObject m(text.data(), text.size());
  if (!m.valid()) {
    *why = std::string("not a JSON object: ") + json_error_str(m.error());
    return false;
  }
  JsonSpan type;
  if (!m.str("msg_type", &type)) {
    *why = "no msg_type";
    return false;
  }
  Reader r(m, why);
  r.u32("message_id", &out->message_id);
  const std::string t(type.p, type.n);
  auto& b = out->body;

  if (t == "hello") {
    out->which_body = statemachined_link_v1_HostMessage_hello_tag;
    r.u32("proto", &b.hello.proto);
    r.hex("seed", &b.hello.seed);
  } else if (t == "set_begin") {
    out->which_body = statemachined_link_v1_HostMessage_set_begin_tag;
    r.u32("set_version", &b.set_begin.set_version);
    r.u32("n_graphs", &b.set_begin.n_graphs);
  } else if (t == "set_end") {
    out->which_body = statemachined_link_v1_HostMessage_set_end_tag;
    r.u32("n_states", &b.set_end.n_states);
    r.u32("n_transitions", &b.set_end.n_transitions);
    r.u32("n_output_actions", &b.set_end.n_output_actions);
    uint64_t checksum = 0;
    r.hex("checksum", &checksum);
    b.set_end.checksum = static_cast<uint32_t>(checksum);
  } else if (t == "graph_begin") {
    out->which_body = statemachined_link_v1_HostMessage_graph_begin_tag;
    r.u32("slot", &b.graph_begin.slot);
    r.u32("n_states", &b.graph_begin.n_states);
    r.u32("entry", &b.graph_begin.entry);
  } else if (t == "graph_dist") {
    out->which_body = statemachined_link_v1_HostMessage_graph_dist_tag;
    auto& d = b.graph_dist;
    r.u32("i", &d.i);
    r.token("kind", &d.kind, kDistKinds);
    r.i32("a", &d.a);
    r.i32("b", &d.b);
    r.i32("c", &d.c);
    r.i32_list("opts", &d.opts_count, d.opts);
    r.u32_list("weights", &d.weights_count, d.weights);
  } else if (t == "graph_state") {
    out->which_body = statemachined_link_v1_HostMessage_graph_state_tag;
    auto& s = b.graph_state;
    r.u32("i", &s.i);
    r.i32("terminal", &s.has_terminal, &s.terminal);
    JsonObject timeout;
    if (r.object("timeout", &timeout)) {
      s.has_timeout = true;
      Reader tr(timeout, why);
      tr.u32("dist", &s.timeout.dist);
      tr.u32("target", &s.timeout.target);
      if (!tr.ok()) return false;
    }
    r.u32("relight", &s.has_relight, &s.relight);
  } else if (t == "graph_transition") {
    out->which_body = statemachined_link_v1_HostMessage_graph_transition_tag;
    auto& x = b.graph_transition;
    r.u32("all", &x.all);
    r.u32("any", &x.any);
    r.u32("none", &x.none);
    r.u32("target", &x.target);
    r.u32("hold", &x.has_hold, &x.hold);
    r.boolean("level", &x.level);
  } else if (t == "graph_action") {
    out->which_body = statemachined_link_v1_HostMessage_graph_action_tag;
    auto& a = b.graph_action;
    r.token("on", &a.on, kActionOn);
    r.u32("line", &a.line);
    r.token("kind", &a.kind, kActionKinds);
    r.u32("ms", &a.ms);
    r.u32("timer", &a.timer);
  } else if (t == "graph_timer") {
    out->which_body = statemachined_link_v1_HostMessage_graph_timer_tag;
    auto& x = b.graph_timer;
    r.u32("i", &x.i);
    r.u32("width", &x.width);
    r.u32("delay", &x.has_delay, &x.delay);
    r.u32("gap", &x.has_gap, &x.gap);
    r.u32("line", &x.has_line, &x.line);
    r.u32("loops", &x.has_loops, &x.loops);
    r.u32("all", &x.all);
    r.u32("any", &x.any);
    r.u32("none", &x.none);
    r.boolean("active_low", &x.active_low);
    r.boolean("trial_bound", &x.trial_bound);
  } else if (t == "graph_end") {
    out->which_body = statemachined_link_v1_HostMessage_graph_end_tag;
    r.u32("n_transitions", &b.graph_end.n_transitions);
    r.u32("n_output_actions", &b.graph_end.n_output_actions);
  } else if (t == "configure") {
    out->which_body = statemachined_link_v1_HostMessage_configure_tag;
    auto& c = b.configure;
    r.u32("trial_id", &c.trial_id);
    r.u32("set_version", &c.set_version);
    r.u32("graph_index", &c.graph_index);
    r.i32("cap_ms", &c.cap_ms);
    r.token("start", &c.start, kStartSources);
    r.u32("start_line", &c.has_start_line, &c.start_line);
    r.u32("timers", &c.has_timers, &c.timers);
    JsonArray patches;
    if (r.array("patch", &patches)) {
      JsonSpan element;
      JsonType element_type = JsonType::Missing;
      while (patches.next(&element, &element_type)) {
        if (element_type != JsonType::Object) {
          r.fail("patch", "an entry is not an object");
          break;
        }
        if (c.patch_count == sizeof(c.patch) / sizeof(c.patch[0])) {
          r.fail("patch", "longer than link.options allows");
          break;
        }
        JsonObject entry(element.p, element.n);
        auto& p = c.patch[c.patch_count++];
        Reader pr(entry, why);
        pr.u32("i", &p.i);
        pr.i32("a", &p.has_a, &p.a);
        pr.i32("b", &p.has_b, &p.b);
        pr.i32("c", &p.has_c, &p.c);
        if (!pr.ok()) return false;
      }
    }
  } else if (t == "start") {
    out->which_body = statemachined_link_v1_HostMessage_start_tag;
    r.u32("trial_id", &b.start.trial_id);
  } else if (t == "cancel") {
    out->which_body = statemachined_link_v1_HostMessage_cancel_tag;
    r.u32("trial_id", &b.cancel.trial_id);
    r.token("reason", &b.cancel.reason, kCancelReasons);
  } else if (t == "ping") {
    out->which_body = statemachined_link_v1_HostMessage_ping_tag;
  } else if (t == "state") {
    out->which_body = statemachined_link_v1_HostMessage_state_tag;
  } else if (t == "save") {
    out->which_body = statemachined_link_v1_HostMessage_save_tag;
  } else if (t == "wiring") {
    out->which_body = statemachined_link_v1_HostMessage_wiring_tag;
    auto& w = b.wiring;
    r.u32("invert", &w.has_invert, &w.invert);
    r.u32("enable", &w.has_enable, &w.enable);
    r.u32("safe", &w.has_safe, &w.safe);
    r.u32_list("debounce_ms", &w.debounce_ms_count, w.debounce_ms);
  } else if (t == "timers") {
    out->which_body = statemachined_link_v1_HostMessage_timers_tag;
    r.u32("enable", &b.timers.enable);
  } else if (t == "pins") {
    out->which_body = statemachined_link_v1_HostMessage_pins_tag;
    r.token("dir", &b.pins.dir, kDirections);
  } else if (t == "autorun") {
    out->which_body = statemachined_link_v1_HostMessage_autorun_tag;
    auto& a = b.autorun;
    r.boolean("enabled", &a.has_enabled, &a.enabled);
    r.u32("graph_index", &a.has_graph_index, &a.graph_index);
    r.i32("cap_ms", &a.has_cap_ms, &a.cap_ms);
    if (r.has("seed")) {
      a.has_seed = true;
      r.hex("seed", &a.seed);
    }
    r.u32("first_trial_id", &a.has_first_trial_id, &a.first_trial_id);
    r.boolean("start_now", &a.has_start_now, &a.start_now);
  } else {
    // A command link.proto has no body for: the board sees a message_id and
    // nothing else, exactly as it would from a newer daemon.
    out->which_body = 0;
  }
  return r.ok();
}

Bytes encode_host_message(const link::HostMessage& message) {
  Bytes out(kMaxPayload);
  pb_ostream_t stream = pb_ostream_from_buffer(out.data(), out.size());
  if (!pb_encode(&stream, statemachined_link_v1_HostMessage_fields, &message)) return {};
  out.resize(stream.bytes_written);
  return out;
}

Bytes frame_of(const Bytes& payload) {
  Bytes out(kMaxFrame);
  out.resize(encode_frame(payload.data(), payload.size(), out.data()));
  return out;
}

bool decode_device_frame(const Bytes& frame, link::DeviceMessage* out, Bytes* payload) {
  FrameReader reader;
  bool got = false;
  for (uint8_t byte : frame) {
    if (!reader.feed(byte)) continue;
    if (got || reader.status() != FrameError::None) return false;
    got = true;
    payload->assign(reader.payload(), reader.payload() + reader.len());
  }
  if (!got) return false;
  *out = statemachined_link_v1_DeviceMessage_init_zero;
  pb_istream_t stream = pb_istream_from_buffer(payload->data(), payload->size());
  return pb_decode(&stream, statemachined_link_v1_DeviceMessage_fields, out);
}

namespace {

void write_row(JsonWriter& w, const link::StateVisit& v) {
  w.elem_u32(v.state);
  w.elem_str(exit_token(v.exit));
  w.elem_u32(v.transition);
  w.elem_i32(v.drawn_ms);
  w.elem_u32(v.entered_us);
  w.elem_u32(v.duration_us);
}

void write_hex16(JsonWriter& w, const char* key, uint32_t value) {
  static const char kHex[] = "0123456789ABCDEF";
  char text[5] = {kHex[(value >> 12) & 0xF], kHex[(value >> 8) & 0xF], kHex[(value >> 4) & 0xF],
                  kHex[value & 0xF], '\0'};
  w.key_str(key, text);
}

}  // namespace

std::string json_of(const link::DeviceMessage& m) {
  char buffer[4096];
  JsonWriter w(buffer, sizeof(buffer));
  const auto& b = m.body;
  const char* type = "?";
  switch (m.which_body) {
    case statemachined_link_v1_DeviceMessage_hello_ack_tag:
      type = "hello_ack";
      break;
    case statemachined_link_v1_DeviceMessage_ack_tag:
      type = "ack";
      break;
    case statemachined_link_v1_DeviceMessage_set_ok_tag:
      type = "set_ok";
      break;
    case statemachined_link_v1_DeviceMessage_armed_tag:
      type = "armed";
      break;
    case statemachined_link_v1_DeviceMessage_started_tag:
      type = "started";
      break;
    case statemachined_link_v1_DeviceMessage_cancel_ack_tag:
      type = "cancel_ack";
      break;
    case statemachined_link_v1_DeviceMessage_result_begin_tag:
      type = "result_begin";
      break;
    case statemachined_link_v1_DeviceMessage_result_path_tag:
      type = "result_path";
      break;
    case statemachined_link_v1_DeviceMessage_result_end_tag:
      type = "result_end";
      break;
    case statemachined_link_v1_DeviceMessage_event_tag:
      type = "event";
      break;
    case statemachined_link_v1_DeviceMessage_error_tag:
      type = "error";
      break;
    case statemachined_link_v1_DeviceMessage_log_tag:
      type = "log";
      break;
    case statemachined_link_v1_DeviceMessage_pong_tag:
      type = "pong";
      break;
    case statemachined_link_v1_DeviceMessage_state_report_tag:
      type = "state_report";
      break;
    case statemachined_link_v1_DeviceMessage_visit_tag:
      type = "visit";
      break;
    case statemachined_link_v1_DeviceMessage_pin_map_tag:
      type = "pin_map";
      break;
    case statemachined_link_v1_DeviceMessage_autorun_ok_tag:
      type = "autorun_ok";
      break;
    case statemachined_link_v1_DeviceMessage_saved_tag:
      type = "saved";
      break;
    default:
      break;
  }
  w.begin(type, m.message_id);
  if (m.has_in_reply_to) w.in_reply_to(m.in_reply_to);

  switch (m.which_body) {
    case statemachined_link_v1_DeviceMessage_hello_ack_tag: {
      const auto& a = b.hello_ack;
      w.key_u32("proto", a.proto);
      w.key_str("board", a.board);
      w.key_str("fw", a.fw);
      w.key_u32("n_input_lines", a.n_input_lines);
      w.key_u32("n_output_lines", a.n_output_lines);
      w.key_u32("scan_hz", a.scan_hz);
      if (a.has_caps) {
        w.begin_object("caps");
        w.key_u32("max_frame", a.caps.max_frame);
        w.key_u32("max_states", a.caps.max_states);
        w.key_u32("max_transitions", a.caps.max_transitions);
        w.key_u32("max_output_actions", a.caps.max_output_actions);
        w.key_u32("max_distributions", a.caps.max_distributions);
        w.key_u32("max_choice_options", a.caps.max_choice_options);
        w.key_u32("max_path", a.caps.max_path);
        w.key_u32("max_graphs", a.caps.max_graphs);
        w.key_u32("max_timers", a.caps.max_timers);
        w.key_u32("first_timer_line", a.caps.first_timer_line);
        w.end_object();
      }
      w.key_bool("has_set", a.has_set);
      w.key_u32("set_version", a.set_version);
      w.key_u32("n_graphs", a.n_graphs);
      w.key_bool("has_wiring", a.has_wiring);
      break;
    }
    case statemachined_link_v1_DeviceMessage_ack_tag:
      if (b.ack.has_enable) w.key_u32("enable", b.ack.enable);
      if (b.ack.has_n_timers) w.key_u32("n_timers", b.ack.n_timers);
      break;
    case statemachined_link_v1_DeviceMessage_set_ok_tag:
      w.key_u32("set_version", b.set_ok.set_version);
      w.key_u32("n_graphs", b.set_ok.n_graphs);
      w.key_u32("n_states", b.set_ok.n_states);
      w.key_u32("n_transitions", b.set_ok.n_transitions);
      w.key_u32("n_output_actions", b.set_ok.n_output_actions);
      break;
    case statemachined_link_v1_DeviceMessage_armed_tag:
      w.key_u32("trial_id", b.armed.trial_id);
      w.key_u32("set_version", b.armed.set_version);
      w.key_u32("graph_index", b.armed.graph_index);
      break;
    case statemachined_link_v1_DeviceMessage_started_tag:
      w.key_u32("trial_id", b.started.trial_id);
      w.key_u32("at_us", b.started.at_us);
      w.key_str("by", token_of(b.started.by, kStartSources));
      if (b.started.has_line) w.key_u32("line", b.started.line);
      break;
    case statemachined_link_v1_DeviceMessage_cancel_ack_tag:
      w.key_u32("trial_id", b.cancel_ack.trial_id);
      w.key_bool("cancelled", b.cancel_ack.cancelled);
      w.key_i32("outcome", b.cancel_ack.outcome);
      break;
    case statemachined_link_v1_DeviceMessage_result_begin_tag: {
      const auto& r = b.result_begin;
      w.key_u32("trial_id", r.trial_id);
      w.key_i32("outcome", r.outcome);
      w.key_u32("cancel_reason", r.cancel_reason);
      w.key_u32("total_us", r.total_us);
      w.key_u32("path_len", r.path_len);
      w.key_u32("first_seq", r.first_seq);
      w.key_u32("total_visits", r.total_visits);
      w.key_bool("truncated", r.truncated);
      break;
    }
    case statemachined_link_v1_DeviceMessage_result_path_tag:
      w.key_u32("trial_id", b.result_path.trial_id);
      w.key_u32("from", b.result_path.from);
      w.begin_array("p");
      for (pb_size_t i = 0; i < b.result_path.p_count; ++i) {
        w.begin_elem_array();
        write_row(w, b.result_path.p[i]);
        w.end_array();
      }
      w.end_array();
      break;
    case statemachined_link_v1_DeviceMessage_result_end_tag:
      w.key_u32("trial_id", b.result_end.trial_id);
      write_hex16(w, "checksum", b.result_end.checksum);
      break;
    case statemachined_link_v1_DeviceMessage_event_tag:
      w.key_u32("us", b.event.us);
      w.key_u32("word", b.event.word);
      break;
    case statemachined_link_v1_DeviceMessage_error_tag:
      w.key_str("code", b.error.code);
      w.key_str("message", b.error.message);
      w.key_str("context", b.error.context);
      break;
    case statemachined_link_v1_DeviceMessage_log_tag:
      w.key_str("level", b.log.level);
      w.key_str("message", b.log.message);
      break;
    case statemachined_link_v1_DeviceMessage_pong_tag:
      w.key_u32("up_us", b.pong.up_us);
      w.key_u32("us", b.pong.us);
      break;
    case statemachined_link_v1_DeviceMessage_state_report_tag: {
      const auto& r = b.state_report;
      w.key_u32("link_state", r.link_state);
      w.begin_object("graph");
      w.key_bool("has_set", r.graph.has_set);
      w.key_u32("set_version", r.graph.set_version);
      w.key_u32("n_graphs", r.graph.n_graphs);
      w.key_u32("index", r.graph.index);
      w.end_object();
      w.key_bool("has_wiring", r.has_wiring);
      w.key_bool("autorun", r.autorun);
      w.key_u32("trial_id", r.trial_id);
      w.key_bool("running", r.running);
      w.key_u32("current_state", r.current_state);
      w.key_u32("up_us", r.up_us);
      w.key_u32("dropped_lines", r.dropped_lines);
      w.key_u32("bad_lines", r.bad_lines);
      w.begin_object("io");
      w.key_u32("in", r.io.in);
      w.key_u32("out", r.io.out);
      w.end_object();
      w.begin_object("scan");
      w.key_u32("hz", r.scan.hz);
      w.key_u32("overruns", r.scan.overruns);
      w.key_u32("worst_gap", r.scan.worst_gap);
      w.key_u32("tx_stalls", r.scan.tx_stalls);
      w.key_u32("visits_dropped", r.scan.visits_dropped);
      w.key_u32("timers_enabled", r.scan.timers_enabled);
      w.key_u32("timers_running", r.scan.timers_running);
      w.end_object();
      break;
    }
    case statemachined_link_v1_DeviceMessage_visit_tag:
      w.key_u32("trial_id", b.visit.trial_id);
      w.key_u32("seq", b.visit.seq);
      w.begin_array("v");
      write_row(w, b.visit.v);
      w.end_array();
      break;
    case statemachined_link_v1_DeviceMessage_pin_map_tag:
      w.key_str("dir", token_of(b.pin_map.dir, kDirections));
      w.key_u32("n", b.pin_map.n);
      w.begin_array("pins");
      for (pb_size_t i = 0; i < b.pin_map.pins_count; ++i) w.elem_str(b.pin_map.pins[i]);
      w.end_array();
      break;
    case statemachined_link_v1_DeviceMessage_autorun_ok_tag:
      w.key_bool("enabled", b.autorun_ok.enabled);
      w.key_bool("active", b.autorun_ok.active);
      w.key_u32("graph_index", b.autorun_ok.graph_index);
      w.key_i32("cap_ms", b.autorun_ok.cap_ms);
      w.key_u32("next_trial_id", b.autorun_ok.next_trial_id);
      break;
    case statemachined_link_v1_DeviceMessage_saved_tag:
      w.key_bool("has_set", b.saved.has_set);
      w.key_u32("set_version", b.saved.set_version);
      w.key_bool("autorun", b.saved.autorun);
      w.key_u32("write_count", b.saved.write_count);
      w.key_bool("written", b.saved.written);
      break;
    default:
      break;
  }
  const size_t n = w.finish(true);
  return std::string(buffer, n);
}

}  // namespace statemachined::test
