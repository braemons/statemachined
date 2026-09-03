// SPDX-License-Identifier: GPL-3.0-or-later
#include "protocol/graph_builder.h"

#include "protocol/crc16.h"

namespace statemachined {
namespace {

/// An optional member: absent leaves `*out` alone, present-but-wrong is an
/// error. Absent and wrong are different here, unlike at the JSON layer, because
/// most of these fields have a documented default and only a malformed one is
/// worth refusing an upload over.
bool opt_u32(const JsonObject& m, const char* key, uint32_t* out, bool* bad) {
  if (m.type_of(key) == JsonType::Missing) return false;
  if (!m.u32(key, out)) {
    *bad = true;
    return false;
  }
  return true;
}

bool opt_i32(const JsonObject& m, const char* key, int32_t* out, bool* bad) {
  if (m.type_of(key) == JsonType::Missing) return false;
  if (!m.i32(key, out)) {
    *bad = true;
    return false;
  }
  return true;
}

}  // namespace

const char* upload_error_str(UploadError e) {
  switch (e) {
    case UploadError::None:
      return "ok";
    case UploadError::NotOpen:
      return "no graph upload is open";
    case UploadError::BadOrder:
      return "graph message out of order";
    case UploadError::BadIndex:
      return "index does not follow the ones already accepted";
    case UploadError::TooMany:
      return "graph exceeds a capacity of this board";
    case UploadError::BadField:
      return "a member is missing, mistyped or out of range";
    case UploadError::CountMismatch:
      return "graph_end's totals disagree with what arrived";
    case UploadError::ChecksumMismatch:
      return "a graph message went missing";
    case UploadError::Invalid:
      return "the assembled graph is not runnable";
  }
  return "unknown";
}

UploadError GraphBuilder::fail(UploadError e, const char* what) {
  context_ = what;
  // A refused message abandons the whole upload rather than leaving it
  // half-built for the next one to add to. The host has to start again, which
  // is cheap, and the alternative is a set assembled from two different
  // attempts. The target is left invalid, so the board holds no graph until a
  // set_end succeeds.
  open_ = false;
  graph_open_ = false;
  complete_ = false;
  return e;
}

void GraphBuilder::fold(JsonSpan covered) {
  checksum_ = crc16_ccitt(covered.p, covered.n, checksum_);
}

UploadError GraphBuilder::begin_set(const JsonObject& m, JsonSpan covered) {
  g_ = GraphSet{};
  checksum_ = 0xFFFF;
  current_state_ = kNoState;
  first_state_ = 0;
  seen_exit_action_ = false;
  open_ = false;
  graph_open_ = false;
  complete_ = false;
  graph_error_ = GraphError::None;
  context_ = "";

  uint16_t version = 0;
  uint8_t n_graphs = 0;
  if (!m.u16("set_version", &version)) return fail(UploadError::BadField, "set_version");
  if (!m.u8("n_graphs", &n_graphs)) return fail(UploadError::BadField, "n_graphs");

  // Declared up front so an oversize set is refused before the first graph is
  // sent rather than after the last -- the same reason graph_begin declares its
  // state count, one level up.
  if (n_graphs == 0) return fail(UploadError::BadField, "n_graphs is zero");
  if (n_graphs > kMaxGraphs) return fail(UploadError::TooMany, "max_graphs");

  g_.version = version;
  expected_graphs_ = n_graphs;
  open_ = true;
  fold(covered);
  return UploadError::None;
}

UploadError GraphBuilder::begin_graph(const JsonObject& m, JsonSpan covered) {
  if (!open_) return fail(UploadError::NotOpen, "graph_begin");
  if (graph_open_) return fail(UploadError::BadOrder, "graph_begin before graph_end");
  if (g_.n_graphs >= expected_graphs_)
    return fail(UploadError::BadIndex, "more graphs than set_begin declared");

  uint8_t slot = 0;
  uint8_t n_states = 0;
  uint8_t entry = 0;
  if (!m.u8("slot", &slot)) return fail(UploadError::BadField, "slot");
  if (!m.u8("n_states", &n_states)) return fail(UploadError::BadField, "n_states");
  if (!m.u8("entry", &entry)) return fail(UploadError::BadField, "entry");

  // Stated rather than implied by arrival order, for the reason graph_state's
  // `i` is -- and it must be the next one, because a graph's states are a
  // contiguous slice of the shared pool.
  if (slot != g_.n_graphs) return fail(UploadError::BadIndex, "slot");

  // Declared up front so an oversize graph is refused before the first state is
  // sent rather than after the last.
  if (n_states == 0) return fail(UploadError::BadField, "n_states is zero");
  if (static_cast<uint16_t>(g_.n_states) + n_states > kMaxStates)
    return fail(UploadError::TooMany, "max_states");
  if (entry >= n_states) return fail(UploadError::BadField, "entry");

  first_state_ = g_.n_states;
  graph_first_transition_ = g_.n_transitions;
  graph_first_action_ = g_.n_output_actions;
  GraphEntry& e = g_.graphs[slot];
  e.first_state = first_state_;
  e.n_states = 0;  // grows as states arrive; end_graph checks it
  e.entry = static_cast<StateIndex>(first_state_ + entry);

  expected_states_ = n_states;
  current_state_ = kNoState;
  seen_exit_action_ = false;
  graph_open_ = true;
  fold(covered);
  return UploadError::None;
}

UploadError GraphBuilder::add_distribution(const JsonObject& m, JsonSpan covered) {
  // A distribution belongs to the SET, not to a graph: the pool is shared, and
  // a graph reusing another's foreperiod is the point of sharing it. So a host
  // may send them all at set level, before the first graph_begin, or with the
  // graph that introduces them.
  //
  // The one rule is the one that was always here: a distribution comes before
  // any state that could name it, so a state is range-checked against the pool
  // as it arrives rather than after the fact.
  if (!open_) return fail(UploadError::NotOpen, "graph_dist");
  if (graph_open_ && g_.graphs[g_.n_graphs].n_states > 0)
    return fail(UploadError::BadOrder, "graph_dist after graph_state");

  uint8_t i = 0;
  if (!m.u8("i", &i)) return fail(UploadError::BadField, "i");
  // Stated rather than implied by arrival order: four bytes, and it turns a
  // dropped message from a silently mis-indexed graph into a refusal.
  if (i != g_.n_distributions) return fail(UploadError::BadIndex, "graph_dist i");
  if (g_.n_distributions >= kMaxDistributions)
    return fail(UploadError::TooMany, "max_distributions");

  JsonSpan kind;
  if (!m.str("kind", &kind)) return fail(UploadError::BadField, "kind");

  RandomDistribution d;
  bool bad = false;
  int32_t a = 0, b = 0, c = 0;
  opt_i32(m, "a", &a, &bad);
  opt_i32(m, "b", &b, &bad);
  opt_i32(m, "c", &c, &bad);
  if (bad) return fail(UploadError::BadField, "a/b/c");
  d.a = a;
  d.b = b;
  d.c = c;

  if (json_str_eq(kind, "fixed")) {
    d.kind = RandomDistributionKind::Fixed;
  } else if (json_str_eq(kind, "uniform")) {
    d.kind = RandomDistributionKind::Uniform;
  } else if (json_str_eq(kind, "exponential")) {
    d.kind = RandomDistributionKind::Exponential;
  } else if (json_str_eq(kind, "choice")) {
    d.kind = RandomDistributionKind::Choice;
    JsonArray opts;
    if (!m.array("opts", &opts)) return fail(UploadError::BadField, "opts");

    const uint8_t first = g_.n_choice_options;
    uint8_t n = 0;
    int32_t ms = 0;
    while (opts.next_i32(&ms)) {
      if (first + n >= kMaxChoiceOptions)
        return fail(UploadError::TooMany, "max_choice_options");
      g_.choice_options[first + n] = ms;
      g_.choice_weights[first + n] = 1;
      ++n;
    }
    if (n == 0) return fail(UploadError::BadField, "opts is empty");

    if (m.type_of("weights") != JsonType::Missing) {
      JsonArray w;
      if (!m.array("weights", &w)) return fail(UploadError::BadField, "weights");
      uint8_t k = 0;
      uint32_t weight = 0;
      while (w.next_u32(&weight)) {
        if (k >= n) return fail(UploadError::BadField, "weights longer than opts");
        if (weight > UINT16_MAX) return fail(UploadError::BadField, "weight");
        g_.choice_weights[first + k] = static_cast<uint16_t>(weight);
        ++k;
      }
      // A short weights array would silently give the unlisted options weight 1
      // against neighbours weighted in the hundreds. Refuse instead.
      if (k != n) return fail(UploadError::BadField, "weights shorter than opts");
      d.weights = &g_.choice_weights[first];
    }
    d.n = n;
    d.opts = &g_.choice_options[first];
    g_.n_choice_options = static_cast<uint8_t>(first + n);
  } else {
    return fail(UploadError::BadField, "kind");
  }

  g_.distributions[g_.n_distributions++] = d;
  fold(covered);
  return UploadError::None;
}

UploadError GraphBuilder::add_state(const JsonObject& m, JsonSpan covered) {
  if (!graph_open_) return fail(UploadError::NotOpen, "graph_state");

  uint8_t i = 0;
  if (!m.u8("i", &i)) return fail(UploadError::BadField, "i");
  // Per graph, counted from zero: the host authored this graph and numbered its
  // states the way it wrote them.
  const uint8_t local = static_cast<uint8_t>(g_.n_states - first_state_);
  if (i != local) return fail(UploadError::BadIndex, "graph_state i");
  if (local >= expected_states_)
    return fail(UploadError::BadIndex, "more states than graph_begin declared");

  State s;
  // The slices start empty and grow as this state's transitions and actions
  // arrive. Everything that follows attaches here until the next graph_state.
  s.first_transition = g_.n_transitions;
  s.first_entry_action = g_.n_output_actions;
  s.first_exit_action = g_.n_output_actions;

  if (m.type_of("terminal") == JsonType::Missing)
    return fail(UploadError::BadField, "terminal");
  if (!m.is_null("terminal")) {
    int8_t code = 0;
    if (!m.i8("terminal", &code)) return fail(UploadError::BadField, "terminal");
    if (code == kNotTerminal)
      return fail(UploadError::BadField, "terminal is the not-terminal code");
    s.terminal_code = code;
  }

  if (m.type_of("timeout") == JsonType::Missing) return fail(UploadError::BadField, "timeout");
  if (!m.is_null("timeout")) {
    JsonObject t;
    if (!m.object("timeout", &t)) return fail(UploadError::BadField, "timeout");
    uint8_t dist = 0, target = 0;
    if (!t.u8("dist", &dist)) return fail(UploadError::BadField, "timeout.dist");
    if (!t.u8("target", &target)) return fail(UploadError::BadField, "timeout.target");
    if (dist >= g_.n_distributions) return fail(UploadError::BadIndex, "timeout.dist");
    if (target >= expected_states_) return fail(UploadError::BadIndex, "timeout.target");
    s.timeout_duration = dist;
    s.timeout_target = static_cast<StateIndex>(first_state_ + target);
  }

  g_.states[g_.n_states++] = s;
  ++g_.graphs[g_.n_graphs].n_states;
  current_state_ = static_cast<StateIndex>(first_state_ + i);
  seen_exit_action_ = false;
  fold(covered);
  return UploadError::None;
}

UploadError GraphBuilder::add_transition(const JsonObject& m, JsonSpan covered) {
  if (!graph_open_) return fail(UploadError::NotOpen, "graph_transition");
  if (current_state_ == kNoState)
    return fail(UploadError::BadOrder, "graph_transition before any graph_state");
  if (g_.n_transitions >= kMaxTransitions) return fail(UploadError::TooMany, "max_transitions");

  Transition t;
  bool bad = false;
  uint32_t v = 0;
  if (opt_u32(m, "all", &v, &bad)) t.all_high = v;
  if (opt_u32(m, "any", &v, &bad)) t.any_high = v;
  if (opt_u32(m, "none", &v, &bad)) t.none_high = v;
  if (bad) return fail(UploadError::BadField, "all/any/none");
  // A transition with no predicate at all would fire on the first evaluation of
  // every state it is in, which is never what an experimenter meant to write.
  if (t.all_high == 0 && t.any_high == 0 && t.none_high == 0)
    return fail(UploadError::BadField, "transition has no predicate");

  uint8_t target = 0;
  if (!m.u8("target", &target)) return fail(UploadError::BadField, "target");
  if (target >= expected_states_) return fail(UploadError::BadIndex, "target");
  // A transition may not leave the graph it belongs to: selecting a graph by
  // index has to select a machine, not a doorway into somebody else's.
  t.target_state = static_cast<StateIndex>(first_state_ + target);

  if (m.type_of("hold") != JsonType::Missing && !m.is_null("hold")) {
    uint8_t hold = 0;
    if (!m.u8("hold", &hold)) return fail(UploadError::BadField, "hold");
    if (hold >= g_.n_distributions) return fail(UploadError::BadIndex, "hold");
    t.hold_duration = hold;
  }

  bool level = false;
  if (m.type_of("level") != JsonType::Missing) {
    if (!m.boolean("level", &level)) return fail(UploadError::BadField, "level");
  }
  t.fire_if_true_on_entry = level;

  g_.transitions[g_.n_transitions++] = t;
  g_.states[current_state_].transition_count++;
  fold(covered);
  return UploadError::None;
}

UploadError GraphBuilder::add_action(const JsonObject& m, JsonSpan covered) {
  if (!graph_open_) return fail(UploadError::NotOpen, "graph_action");
  if (current_state_ == kNoState)
    return fail(UploadError::BadOrder, "graph_action before any graph_state");
  if (g_.n_output_actions >= kMaxOutputActions)
    return fail(UploadError::TooMany, "max_output_actions");

  JsonSpan on;
  if (!m.str("on", &on)) return fail(UploadError::BadField, "on");
  const bool is_entry = json_str_eq(on, "entry");
  if (!is_entry && !json_str_eq(on, "exit")) return fail(UploadError::BadField, "on");

  // Entry and exit actions are two slices of the same pool, so each has to be
  // contiguous: all of a state's entry actions must arrive before its first
  // exit action. Interleaving them would silently give one slice the other's
  // members.
  if (is_entry && seen_exit_action_)
    return fail(UploadError::BadOrder, "entry action after an exit action");

  OutputAction a;
  uint8_t line = 0;
  if (!m.u8("line", &line)) return fail(UploadError::BadField, "line");
  if (line >= kMaxOutputLines) return fail(UploadError::BadField, "line");
  a.output_line = line;

  JsonSpan kind;
  if (!m.str("kind", &kind)) return fail(UploadError::BadField, "kind");
  if (json_str_eq(kind, "high")) {
    a.kind = OutputActionKind::High;
  } else if (json_str_eq(kind, "low")) {
    a.kind = OutputActionKind::Low;
  } else if (json_str_eq(kind, "toggle")) {
    a.kind = OutputActionKind::Toggle;
  } else if (json_str_eq(kind, "pulse")) {
    a.kind = OutputActionKind::Pulse;
    uint32_t ms = 0;
    bool bad = false;
    if (!opt_u32(m, "ms", &ms, &bad) || bad) return fail(UploadError::BadField, "pulse ms");
    if (ms == 0 || ms > UINT16_MAX) return fail(UploadError::BadField, "pulse ms");
    a.pulse_ms = static_cast<NarrowMilliseconds>(ms);
  } else {
    return fail(UploadError::BadField, "kind");
  }

  State& s = g_.states[current_state_];
  if (is_entry) {
    g_.output_actions[g_.n_output_actions++] = a;
    s.entry_action_count++;
    s.first_exit_action = g_.n_output_actions;  // exits start after the entries
  } else {
    if (!seen_exit_action_) {
      s.first_exit_action = g_.n_output_actions;
      seen_exit_action_ = true;
    }
    g_.output_actions[g_.n_output_actions++] = a;
    s.exit_action_count++;
  }
  fold(covered);
  return UploadError::None;
}

UploadError GraphBuilder::end_graph(const JsonObject& m, JsonSpan covered) {
  if (!graph_open_) return fail(UploadError::NotOpen, "graph_end");

  uint8_t n_transitions = 0, n_actions = 0;
  if (!m.u8("n_transitions", &n_transitions))
    return fail(UploadError::BadField, "n_transitions");
  if (!m.u8("n_output_actions", &n_actions))
    return fail(UploadError::BadField, "n_output_actions");

  GraphEntry& e = g_.graphs[g_.n_graphs];
  if (e.n_states != expected_states_) return fail(UploadError::CountMismatch, "n_states");
  // Per graph, not per set: a host that miscounted one graph's transitions
  // should be told which graph.
  if (g_.n_transitions - graph_first_transition_ != n_transitions)
    return fail(UploadError::CountMismatch, "n_transitions");
  if (g_.n_output_actions - graph_first_action_ != n_actions)
    return fail(UploadError::CountMismatch, "n_output_actions");

  ++g_.n_graphs;
  graph_open_ = false;
  current_state_ = kNoState;
  fold(covered);
  return UploadError::None;
}

UploadError GraphBuilder::end_set(const JsonObject& m) {
  if (!open_) return fail(UploadError::NotOpen, "set_end");
  if (graph_open_) return fail(UploadError::BadOrder, "set_end before graph_end");
  if (g_.n_graphs != expected_graphs_) return fail(UploadError::CountMismatch, "n_graphs");

  uint8_t n_states = 0, n_transitions = 0, n_actions = 0;
  if (!m.u8("n_states", &n_states)) return fail(UploadError::BadField, "n_states");
  if (!m.u8("n_transitions", &n_transitions))
    return fail(UploadError::BadField, "n_transitions");
  if (!m.u8("n_output_actions", &n_actions))
    return fail(UploadError::BadField, "n_output_actions");

  if (g_.n_states != n_states) return fail(UploadError::CountMismatch, "n_states");
  if (g_.n_transitions != n_transitions)
    return fail(UploadError::CountMismatch, "n_transitions");
  if (g_.n_output_actions != n_actions)
    return fail(UploadError::CountMismatch, "n_output_actions");

  JsonSpan hex;
  if (!m.str("checksum", &hex) || hex.n != 4) return fail(UploadError::BadField, "checksum");
  uint16_t claimed = 0;
  if (!crc16_from_hex(hex.p, &claimed)) return fail(UploadError::BadField, "checksum");
  // The per-line crc catches a corrupt message; this catches a missing one.
  if (claimed != checksum_) return fail(UploadError::ChecksumMismatch, "checksum");

  const GraphError ge = validate(g_);
  if (ge != GraphError::None) {
    graph_error_ = ge;
    return fail(UploadError::Invalid, graph_error_str(ge));
  }

  open_ = false;
  complete_ = true;
  context_ = "";
  return UploadError::None;
}

}  // namespace statemachined
