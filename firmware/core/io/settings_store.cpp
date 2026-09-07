// SPDX-License-Identifier: GPL-3.0-or-later
#include "io/settings_store.h"

#include "protocol/crc16.h"

namespace statemachined {
namespace {

/// "SMD" and a format generation. Checked before anything else is believed:
/// erased data flash reads as 0xFF, which fails this and is reported as
/// BadMagic -- a blank board, not a corrupt one.
constexpr uint8_t kMagic[4] = {'S', 'M', 'D', '1'};

/// Little-endian on the wire to storage, stated rather than inherited. Both
/// ends of this are the same board today, but a record whose byte order is
/// whatever the compiler felt like is a record only that compiler can read.
class Encoder {
 public:
  explicit Encoder(SettingsWriter& out) : out_(out) {}

  void bytes(const void* p, size_t n) {
    if (!ok_) return;
    const uint8_t* b = static_cast<const uint8_t*>(p);
    crc_ = crc16_ccitt(reinterpret_cast<const char*>(b), n, crc_);
    ok_ = out_.write(b, n);
  }
  void u8(uint8_t v) { bytes(&v, 1); }
  void u16(uint16_t v) {
    const uint8_t b[2] = {static_cast<uint8_t>(v), static_cast<uint8_t>(v >> 8)};
    bytes(b, 2);
  }
  void u32(uint32_t v) {
    const uint8_t b[4] = {static_cast<uint8_t>(v), static_cast<uint8_t>(v >> 8),
                          static_cast<uint8_t>(v >> 16), static_cast<uint8_t>(v >> 24)};
    bytes(b, 4);
  }
  void u64(uint64_t v) {
    u32(static_cast<uint32_t>(v));
    u32(static_cast<uint32_t>(v >> 32));
  }
  void i16(int16_t v) { u16(static_cast<uint16_t>(v)); }
  void i32(int32_t v) { u32(static_cast<uint32_t>(v)); }
  void boolean(bool v) { u8(v ? 1u : 0u); }

  /// The CRC is over everything written before it, and is itself the only field
  /// not folded in.
  bool finish() {
    if (!ok_) return false;
    const uint16_t c = crc_;
    const uint8_t b[2] = {static_cast<uint8_t>(c), static_cast<uint8_t>(c >> 8)};
    return out_.write(b, 2);
  }

 private:
  SettingsWriter& out_;
  uint16_t crc_ = 0xFFFF;
  bool ok_ = true;
};

class Decoder {
 public:
  explicit Decoder(SettingsReader& in) : in_(in) {}

  bool ok() const { return ok_; }

  void bytes(void* p, size_t n) {
    if (!ok_) return;
    ok_ = in_.read(p, n);
    if (ok_) crc_ = crc16_ccitt(static_cast<const char*>(p), n, crc_);
  }
  uint8_t u8() {
    uint8_t v = 0;
    bytes(&v, 1);
    return v;
  }
  uint16_t u16() {
    uint8_t b[2] = {0, 0};
    bytes(b, 2);
    return static_cast<uint16_t>(b[0] | (b[1] << 8));
  }
  uint32_t u32() {
    uint8_t b[4] = {0, 0, 0, 0};
    bytes(b, 4);
    return static_cast<uint32_t>(b[0]) | (static_cast<uint32_t>(b[1]) << 8) |
           (static_cast<uint32_t>(b[2]) << 16) | (static_cast<uint32_t>(b[3]) << 24);
  }
  uint64_t u64() {
    const uint64_t lo = u32();
    const uint64_t hi = u32();
    return lo | (hi << 32);
  }
  int16_t i16() { return static_cast<int16_t>(u16()); }
  int32_t i32() { return static_cast<int32_t>(u32()); }
  bool boolean() { return u8() != 0; }

  /// True if the record's own CRC matches everything read before it.
  bool crc_matches() {
    if (!ok_) return false;
    const uint16_t expected = crc_;
    uint8_t b[2] = {0, 0};
    if (!in_.read(b, 2)) return false;
    const uint16_t found = static_cast<uint16_t>(b[0] | (b[1] << 8));
    return found == expected;
  }

 private:
  SettingsReader& in_;
  uint16_t crc_ = 0xFFFF;
  bool ok_ = true;
};

/// Where a Choice distribution's options sit in the set's own pool, or -1 for a
/// distribution that points somewhere else entirely -- which is how a graph
/// built in C++ does it, and which storage cannot follow. Such a distribution
/// is stored with no options and refused at load, rather than being restored
/// with a pointer into somebody else's memory.
int16_t pool_index(const GraphSet& g, const Milliseconds* p) {
  if (p == nullptr) return -1;
  for (uint8_t i = 0; i < g.n_choice_options; ++i)
    if (p == &g.choice_options[i]) return static_cast<int16_t>(i);
  return -1;
}

int16_t weight_index(const GraphSet& g, const uint16_t* p) {
  if (p == nullptr) return -1;
  for (uint8_t i = 0; i < g.n_choice_options; ++i)
    if (p == &g.choice_weights[i]) return static_cast<int16_t>(i);
  return -1;
}

/// A writer that compares rather than writes: the encoder is run again and each
/// byte it produces is matched against the stored one. Returning false at the
/// first difference is what stops the comparison early, since the encoder gives
/// up when a write fails.
class ComparingWriter : public SettingsWriter {
 public:
  explicit ComparingWriter(SettingsReader& in) : in_(in) {}

  bool write(const void* src, size_t n) override {
    const uint8_t* want = static_cast<const uint8_t*>(src);
    while (n > 0) {
      const size_t chunk = n < sizeof(buffer_) ? n : sizeof(buffer_);
      if (!in_.read(buffer_, chunk)) return false;  // the store is shorter
      for (size_t i = 0; i < chunk; ++i)
        if (buffer_[i] != want[i]) return false;
      want += chunk;
      n -= chunk;
    }
    return true;
  }

 private:
  SettingsReader& in_;
  uint8_t buffer_[32];
};

}  // namespace

bool settings_already_stored(const StoredSettings& s, uint32_t write_count,
                             SettingsReader& in) {
  ComparingWriter comparing(in);
  // Every byte matched, CRC included -- and the CRC covers the whole record, so
  // this is a comparison of the content and not only of its shape.
  return save_settings(s, write_count, comparing);
}

const char* settings_error_str(SettingsError e) {
  switch (e) {
    case SettingsError::None:
      return "ok";
    case SettingsError::Io:
      return "storage would not read or write";
    case SettingsError::BadMagic:
      return "no settings record stored";
    case SettingsError::BadVersion:
      return "a settings record this firmware does not read";
    case SettingsError::BadCrc:
      return "the stored settings are damaged";
    case SettingsError::TooBig:
      return "the stored graph set is larger than this build can hold";
  }
  return "unknown";
}

bool save_settings(const StoredSettings& s, uint32_t write_count, SettingsWriter& out) {
  Encoder e(out);
  e.bytes(kMagic, sizeof(kMagic));
  e.u16(kSettingsFormat);
  e.u32(write_count);

  // The wiring. The debounce table is written with its own length rather than
  // as kMaxLines entries, so a record from a build with more lines than this
  // one is refused on the count instead of read as a shorter one.
  e.u32(s.wiring.inputs.invert_mask);
  e.u32(s.wiring.inputs.enable_mask);
  e.u8(kMaxLines);
  for (uint8_t i = 0; i < kMaxLines; ++i) e.u16(s.wiring.inputs.debounce_ms[i]);
  e.u32(s.wiring.output_safe_levels);

  e.boolean(s.autorun.enabled);
  e.u8(s.autorun.graph_index);
  e.i32(s.autorun.cap_ms);
  e.u64(s.autorun.seed);
  e.u32(s.autorun.first_trial_id);

  const bool has_set = s.has_set && s.set != nullptr;
  e.boolean(has_set);
  if (has_set) {
    const GraphSet& g = *s.set;
    e.u16(g.version);
    e.u8(g.n_graphs);
    e.u8(g.n_states);
    e.u8(g.n_transitions);
    e.u8(g.n_output_actions);
    e.u8(g.n_distributions);
    e.u8(g.n_choice_options);

    for (uint8_t i = 0; i < g.n_graphs; ++i) {
      e.u8(g.graphs[i].entry);
      e.u8(g.graphs[i].first_state);
      e.u8(g.graphs[i].n_states);
    }
    for (uint8_t i = 0; i < g.n_states; ++i) {
      const State& st = g.states[i];
      e.u8(st.first_transition);
      e.u8(st.transition_count);
      e.u8(st.first_entry_action);
      e.u8(st.entry_action_count);
      e.u8(st.first_exit_action);
      e.u8(st.exit_action_count);
      e.u8(st.timeout_duration);
      e.u8(st.timeout_target);
      e.u8(static_cast<uint8_t>(st.terminal_code));
      e.u8(st.relight_duration);
    }
    for (uint8_t i = 0; i < g.n_transitions; ++i) {
      const Transition& t = g.transitions[i];
      e.u32(t.all_high);
      e.u32(t.any_high);
      e.u32(t.none_high);
      e.u8(t.target_state);
      e.u8(t.hold_duration);
      e.boolean(t.fire_if_true_on_entry);
    }
    for (uint8_t i = 0; i < g.n_output_actions; ++i) {
      const OutputAction& a = g.output_actions[i];
      e.u8(a.output_line);
      e.u8(static_cast<uint8_t>(a.kind));
      e.u16(a.pulse_ms);
    }
    for (uint8_t i = 0; i < g.n_distributions; ++i) {
      const RandomDistribution& d = g.distributions[i];
      e.u8(static_cast<uint8_t>(d.kind));
      e.u8(d.n);
      e.i32(d.a);
      e.i32(d.b);
      e.i32(d.c);
      // Offsets, not addresses: see the note at the top of the header.
      e.i16(pool_index(g, d.opts));
      e.i16(weight_index(g, d.weights));
    }
    for (uint8_t i = 0; i < g.n_choice_options; ++i) e.i32(g.choice_options[i]);
    for (uint8_t i = 0; i < g.n_choice_options; ++i) e.u16(g.choice_weights[i]);
  }

  return e.finish();
}

SettingsError load_settings(StoredSettings& s, SettingsReader& in) {
  Decoder d(in);

  uint8_t magic[4] = {0, 0, 0, 0};
  d.bytes(magic, sizeof(magic));
  if (!d.ok()) return SettingsError::Io;
  for (size_t i = 0; i < sizeof(magic); ++i)
    if (magic[i] != kMagic[i]) return SettingsError::BadMagic;

  const uint16_t format = d.u16();
  if (!d.ok()) return SettingsError::Io;
  // Refused, never interpreted. A record from another format read as this one
  // is a rig coming up with somebody else's safe levels.
  if (format != kSettingsFormat) return SettingsError::BadVersion;

  const uint32_t write_count = d.u32();

  DeviceWiring wiring;
  wiring.inputs.invert_mask = d.u32();
  wiring.inputs.enable_mask = d.u32();
  const uint8_t n_lines = d.u8();
  if (!d.ok()) return SettingsError::Io;
  if (n_lines != kMaxLines) return SettingsError::TooBig;
  for (uint8_t i = 0; i < n_lines; ++i)
    wiring.inputs.debounce_ms[i] = static_cast<NarrowMilliseconds>(d.u16());
  wiring.output_safe_levels = d.u32();

  AutorunConfig autorun;
  autorun.enabled = d.boolean();
  autorun.graph_index = d.u8();
  autorun.cap_ms = d.i32();
  autorun.seed = d.u64();
  autorun.first_trial_id = d.u32();

  const bool has_set = d.boolean();
  if (!d.ok()) return SettingsError::Io;

  // Read into the caller's set only once the counts are known to fit, so a
  // record that is too big cannot half-overwrite a set the board is using.
  GraphSet* g = s.set;
  if (has_set) {
    if (g == nullptr) return SettingsError::TooBig;
    const uint16_t version = d.u16();
    const uint8_t n_graphs = d.u8();
    const uint8_t n_states = d.u8();
    const uint8_t n_transitions = d.u8();
    const uint8_t n_output_actions = d.u8();
    const uint8_t n_distributions = d.u8();
    const uint8_t n_choice_options = d.u8();
    if (!d.ok()) return SettingsError::Io;
    if (n_graphs > kMaxGraphs || n_states > kMaxStates || n_transitions > kMaxTransitions ||
        n_output_actions > kMaxOutputActions || n_distributions > kMaxDistributions ||
        n_choice_options > kMaxChoiceOptions)
      return SettingsError::TooBig;

    *g = GraphSet{};
    g->version = version;
    g->n_graphs = n_graphs;
    g->n_states = n_states;
    g->n_transitions = n_transitions;
    g->n_output_actions = n_output_actions;
    g->n_distributions = n_distributions;
    g->n_choice_options = n_choice_options;

    for (uint8_t i = 0; i < n_graphs; ++i) {
      g->graphs[i].entry = d.u8();
      g->graphs[i].first_state = d.u8();
      g->graphs[i].n_states = d.u8();
    }
    for (uint8_t i = 0; i < n_states; ++i) {
      State& st = g->states[i];
      st.first_transition = d.u8();
      st.transition_count = d.u8();
      st.first_entry_action = d.u8();
      st.entry_action_count = d.u8();
      st.first_exit_action = d.u8();
      st.exit_action_count = d.u8();
      st.timeout_duration = d.u8();
      st.timeout_target = d.u8();
      st.terminal_code = static_cast<TerminalCode>(d.u8());
      st.relight_duration = d.u8();
    }
    for (uint8_t i = 0; i < n_transitions; ++i) {
      Transition& t = g->transitions[i];
      t.all_high = d.u32();
      t.any_high = d.u32();
      t.none_high = d.u32();
      t.target_state = d.u8();
      t.hold_duration = d.u8();
      t.fire_if_true_on_entry = d.boolean();
    }
    for (uint8_t i = 0; i < n_output_actions; ++i) {
      OutputAction& a = g->output_actions[i];
      a.output_line = d.u8();
      a.kind = static_cast<OutputActionKind>(d.u8());
      a.pulse_ms = static_cast<NarrowMilliseconds>(d.u16());
    }
    // The offsets are held until the option pool has been read: a pointer into
    // an array that is still being filled is a pointer that is right by luck.
    int16_t opt_at[kMaxDistributions];
    int16_t weight_at[kMaxDistributions];
    for (uint8_t i = 0; i < n_distributions; ++i) {
      RandomDistribution& dist = g->distributions[i];
      dist.kind = static_cast<RandomDistributionKind>(d.u8());
      dist.n = d.u8();
      dist.a = d.i32();
      dist.b = d.i32();
      dist.c = d.i32();
      opt_at[i] = d.i16();
      weight_at[i] = d.i16();
    }
    for (uint8_t i = 0; i < n_choice_options; ++i) g->choice_options[i] = d.i32();
    for (uint8_t i = 0; i < n_choice_options; ++i)
      g->choice_weights[i] = static_cast<uint16_t>(d.u16());
    if (!d.ok()) return SettingsError::Io;

    for (uint8_t i = 0; i < n_distributions; ++i) {
      RandomDistribution& dist = g->distributions[i];
      dist.opts = (opt_at[i] >= 0 && opt_at[i] < n_choice_options)
                      ? &g->choice_options[opt_at[i]]
                      : nullptr;
      dist.weights = (weight_at[i] >= 0 && weight_at[i] < n_choice_options)
                         ? &g->choice_weights[weight_at[i]]
                         : nullptr;
    }
  }

  if (!d.ok()) return SettingsError::Io;
  // Last, and it decides everything: an interrupted save reads back as a
  // damaged record, which is a board that has forgotten its settings rather
  // than one that has invented some.
  if (!d.crc_matches()) return SettingsError::BadCrc;

  s.wiring = wiring;
  s.autorun = autorun;
  s.has_set = has_set;
  s.write_count = write_count;
  return SettingsError::None;
}

}  // namespace statemachined
