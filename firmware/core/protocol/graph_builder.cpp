// SPDX-License-Identifier: GPL-3.0-or-later
#include "protocol/graph_builder.h"

#include "protocol/crc16.h"

namespace statemachined {
namespace {

/// A wire value that has to fit a byte. protobuf carries every index as a
/// uint32, and a value the board would silently truncate -- state 256 read as
/// state 0 -- is a field out of range, refused like one.
bool narrow_u8(uint32_t value, uint8_t* out) {
  if (value > UINT8_MAX) return false;
  *out = static_cast<uint8_t>(value);
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

void GraphBuilder::fold(link::PayloadSpan payload) {
  checksum_ = crc16_ccitt(payload.bytes, payload.len, checksum_);
}

UploadError GraphBuilder::begin_set(const link::SetBegin& m, link::PayloadSpan payload) {
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

  uint8_t n_graphs = 0;
  if (m.set_version > UINT16_MAX) return fail(UploadError::BadField, "set_version");
  const uint16_t version = static_cast<uint16_t>(m.set_version);
  if (!narrow_u8(m.n_graphs, &n_graphs)) return fail(UploadError::BadField, "n_graphs");

  // Declared up front so an oversize set is refused before the first graph is
  // sent rather than after the last -- the same reason graph_begin declares its
  // state count, one level up.
  if (n_graphs == 0) return fail(UploadError::BadField, "n_graphs is zero");
  if (n_graphs > kMaxGraphs) return fail(UploadError::TooMany, "max_graphs");

  g_.version = version;
  expected_graphs_ = n_graphs;
  open_ = true;
  fold(payload);
  return UploadError::None;
}

UploadError GraphBuilder::begin_graph(const link::GraphBegin& m, link::PayloadSpan payload) {
  if (!open_) return fail(UploadError::NotOpen, "graph_begin");
  if (graph_open_) return fail(UploadError::BadOrder, "graph_begin before graph_end");
  if (g_.n_graphs >= expected_graphs_)
    return fail(UploadError::BadIndex, "more graphs than set_begin declared");

  uint8_t slot = 0;
  uint8_t n_states = 0;
  uint8_t entry = 0;
  if (!narrow_u8(m.slot, &slot)) return fail(UploadError::BadField, "slot");
  if (!narrow_u8(m.n_states, &n_states)) return fail(UploadError::BadField, "n_states");
  if (!narrow_u8(m.entry, &entry)) return fail(UploadError::BadField, "entry");

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
  fold(payload);
  return UploadError::None;
}

UploadError GraphBuilder::add_distribution(const link::GraphDist& m, link::PayloadSpan payload) {
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

  // Stated rather than implied by arrival order: a byte, and it turns a
  // dropped message from a silently mis-indexed graph into a refusal.
  if (m.i != g_.n_distributions) return fail(UploadError::BadIndex, "graph_dist i");
  if (g_.n_distributions >= kMaxDistributions)
    return fail(UploadError::TooMany, "max_distributions");

  RandomDistribution d;
  d.a = m.a;
  d.b = m.b;
  d.c = m.c;

  switch (m.kind) {
    case statemachined_link_v1_DistKind_DIST_KIND_FIXED:
      d.kind = RandomDistributionKind::Fixed;
      break;
    case statemachined_link_v1_DistKind_DIST_KIND_UNIFORM:
      d.kind = RandomDistributionKind::Uniform;
      break;
    case statemachined_link_v1_DistKind_DIST_KIND_EXPONENTIAL:
      d.kind = RandomDistributionKind::Exponential;
      break;
    case statemachined_link_v1_DistKind_DIST_KIND_CHOICE: {
      d.kind = RandomDistributionKind::Choice;
      const uint8_t first = g_.n_choice_options;
      // More options than the pool has room left for. nanopb has already
      // refused a list longer than the whole pool, so this is the one that
      // knows how much of it the earlier distributions took.
      if (first + m.opts_count > kMaxChoiceOptions)
        return fail(UploadError::TooMany, "max_choice_options");
      const uint8_t n = static_cast<uint8_t>(m.opts_count);
      if (n == 0) return fail(UploadError::BadField, "opts is empty");
      for (uint8_t k = 0; k < n; ++k) {
        g_.choice_options[first + k] = m.opts[k];
        g_.choice_weights[first + k] = 1;
      }

      // Empty weights means every option weighs 1 -- protobuf cannot tell an
      // empty list from an absent one, and neither needs to be told apart.
      if (m.weights_count != 0) {
        if (m.weights_count > n) return fail(UploadError::BadField, "weights longer than opts");
        // A short weights array would silently give the unlisted options weight
        // 1 against neighbours weighted in the hundreds. Refuse instead.
        if (m.weights_count < n) return fail(UploadError::BadField, "weights shorter than opts");
        for (uint8_t k = 0; k < n; ++k) {
          if (m.weights[k] > UINT16_MAX) return fail(UploadError::BadField, "weight");
          g_.choice_weights[first + k] = static_cast<uint16_t>(m.weights[k]);
        }
        d.weights = &g_.choice_weights[first];
      }
      d.n = n;
      d.opts = &g_.choice_options[first];
      g_.n_choice_options = static_cast<uint8_t>(first + n);
      break;
    }
    default:
      return fail(UploadError::BadField, "kind");
  }

  g_.distributions[g_.n_distributions++] = d;
  fold(payload);
  return UploadError::None;
}

UploadError GraphBuilder::add_state(const link::GraphState& m, link::PayloadSpan payload) {
  if (!graph_open_) return fail(UploadError::NotOpen, "graph_state");

  // Per graph, counted from zero: the host authored this graph and numbered its
  // states the way it wrote them.
  const uint8_t local = static_cast<uint8_t>(g_.n_states - first_state_);
  if (m.i != local) return fail(UploadError::BadIndex, "graph_state i");
  if (local >= expected_states_)
    return fail(UploadError::BadIndex, "more states than graph_begin declared");

  State s;
  // The slices start empty and grow as this state's transitions and actions
  // arrive. Everything that follows attaches here until the next graph_state.
  s.first_transition = g_.n_transitions;
  s.first_entry_action = g_.n_output_actions;
  s.first_exit_action = g_.n_output_actions;

  // Absent means not terminal. A code that does not fit the board's signed
  // byte is out of range rather than truncated into some other outcome.
  if (m.has_terminal) {
    if (m.terminal < INT8_MIN || m.terminal > INT8_MAX)
      return fail(UploadError::BadField, "terminal");
    const TerminalCode code = static_cast<TerminalCode>(m.terminal);
    if (code == kNotTerminal)
      return fail(UploadError::BadField, "terminal is the not-terminal code");
    s.terminal_code = code;
  }

  if (m.has_timeout) {
    uint8_t dist = 0, target = 0;
    if (!narrow_u8(m.timeout.dist, &dist)) return fail(UploadError::BadField, "timeout.dist");
    if (!narrow_u8(m.timeout.target, &target))
      return fail(UploadError::BadField, "timeout.target");
    if (dist >= g_.n_distributions) return fail(UploadError::BadIndex, "timeout.dist");
    if (target >= expected_states_) return fail(UploadError::BadIndex, "timeout.target");
    s.timeout_duration = dist;
    s.timeout_target = static_cast<StateIndex>(first_state_ + target);
  }

  // Absent means none: a state that says nothing about relighting is saying
  // the thing every graph written before this existed says.
  if (m.has_relight) {
    uint8_t dist = 0;
    if (!narrow_u8(m.relight, &dist)) return fail(UploadError::BadField, "relight");
    if (dist >= g_.n_distributions) return fail(UploadError::BadIndex, "relight");
    // Refused here as well as in validate(), because this one can say which
    // message was wrong while the upload is still open.
    if (s.terminal_code == kNotTerminal)
      return fail(UploadError::BadField, "relight on a state that is not terminal");
    s.relight_duration = dist;
  }

  g_.states[g_.n_states++] = s;
  ++g_.graphs[g_.n_graphs].n_states;
  current_state_ = static_cast<StateIndex>(first_state_ + local);
  seen_exit_action_ = false;
  fold(payload);
  return UploadError::None;
}

UploadError GraphBuilder::add_transition(const link::GraphTransition& m,
                                         link::PayloadSpan payload) {
  if (!graph_open_) return fail(UploadError::NotOpen, "graph_transition");
  if (current_state_ == kNoState)
    return fail(UploadError::BadOrder, "graph_transition before any graph_state");
  if (g_.n_transitions >= kMaxTransitions) return fail(UploadError::TooMany, "max_transitions");

  Transition t;
  t.all_high = m.all;
  t.any_high = m.any;
  t.none_high = m.none;
  // A transition with no predicate at all would fire on the first evaluation of
  // every state it is in, which is never what an experimenter meant to write.
  if (t.all_high == 0 && t.any_high == 0 && t.none_high == 0)
    return fail(UploadError::BadField, "transition has no predicate");

  uint8_t target = 0;
  if (!narrow_u8(m.target, &target)) return fail(UploadError::BadField, "target");
  if (target >= expected_states_) return fail(UploadError::BadIndex, "target");
  // A transition may not leave the graph it belongs to: selecting a graph by
  // index has to select a machine, not a doorway into somebody else's.
  t.target_state = static_cast<StateIndex>(first_state_ + target);

  if (m.has_hold) {
    if (m.hold > UINT8_MAX) return fail(UploadError::BadField, "hold");
    if (m.hold >= g_.n_distributions) return fail(UploadError::BadIndex, "hold");
    t.hold_duration = static_cast<RandomDistributionIndex>(m.hold);
  }

  t.fire_if_true_on_entry = m.level;

  g_.transitions[g_.n_transitions++] = t;
  g_.states[current_state_].transition_count++;
  fold(payload);
  return UploadError::None;
}

UploadError GraphBuilder::add_timer(const link::GraphTimer& m, link::PayloadSpan payload) {
  // Inside a graph block, like `graph_dist`, and into a pool that belongs to
  // the whole set. The nesting is about ordering, not ownership: a timer names
  // distributions, so it has to arrive after the ones it names, and a graph
  // block is where distributions are declared. What it *means* is set-scope --
  // a timer outlives the run that started it, so it cannot belong to one graph.
  // See graph/global_timer.h.
  if (!graph_open_) return fail(UploadError::NotOpen, "graph_timer");
  if (g_.n_timers >= kMaxTimers) return fail(UploadError::TooMany, "max_timers");

  // Declared in order, like every other pooled thing, so that a set cannot
  // leave a hole -- an undeclared timer between two declared ones would be one
  // an action could name and nothing would ever run.
  if (m.i != g_.n_timers) return fail(UploadError::BadOrder, "timers must be declared in order");

  GlobalTimer t;
  t.all_high = m.all;
  t.any_high = m.any;
  t.none_high = m.none;
  // Unlike a transition, no predicate at all is legal and means "started only
  // by a timer_start action". The bank never evaluates a timer with no masks,
  // so the vacuously-true predicate that would fire it every scan is not
  // reachable. See has_trigger() in machine/global_timers.cpp.

  // The width is the one duration a timer cannot do without: a timer that is
  // never high is a line that never moves and a predicate that never fires.
  if (m.width > UINT8_MAX) return fail(UploadError::BadField, "width");
  if (m.width >= g_.n_distributions) return fail(UploadError::BadIndex, "width");
  t.width = static_cast<RandomDistributionIndex>(m.width);

  if (m.has_delay) {
    if (m.delay > UINT8_MAX) return fail(UploadError::BadField, "delay");
    if (m.delay >= g_.n_distributions) return fail(UploadError::BadIndex, "delay");
    t.delay = static_cast<RandomDistributionIndex>(m.delay);
  }
  if (m.has_gap) {
    if (m.gap > UINT8_MAX) return fail(UploadError::BadField, "gap");
    if (m.gap >= g_.n_distributions) return fail(UploadError::BadIndex, "gap");
    t.gap = static_cast<RandomDistributionIndex>(m.gap);
  }

  if (m.has_line) {
    if (m.line >= kMaxOutputLines) return fail(UploadError::BadField, "line");
    t.output_line = static_cast<LineIndex>(m.line);
  }

  // Absent means the default of one pulse; zero is a timer that runs until
  // something stops it, which is why this one field has presence.
  if (m.has_loops) {
    if (m.loops > UINT8_MAX) return fail(UploadError::BadField, "loops");
    t.loops = static_cast<uint8_t>(m.loops);
  }

  t.active_low = m.active_low;
  t.trial_bound = m.trial_bound;

  g_.timers[g_.n_timers++] = t;
  fold(payload);
  return UploadError::None;
}

UploadError GraphBuilder::add_action(const link::GraphAction& m, link::PayloadSpan payload) {
  if (!graph_open_) return fail(UploadError::NotOpen, "graph_action");
  if (current_state_ == kNoState)
    return fail(UploadError::BadOrder, "graph_action before any graph_state");
  if (g_.n_output_actions >= kMaxOutputActions)
    return fail(UploadError::TooMany, "max_output_actions");

  const bool is_entry = m.on == statemachined_link_v1_ActionOn_ACTION_ON_ENTRY;
  if (!is_entry && m.on != statemachined_link_v1_ActionOn_ACTION_ON_EXIT)
    return fail(UploadError::BadField, "on");

  // Entry and exit actions are two slices of the same pool, so each has to be
  // contiguous: all of a state's entry actions must arrive before its first
  // exit action. Interleaving them would silently give one slice the other's
  // members.
  if (is_entry && seen_exit_action_)
    return fail(UploadError::BadOrder, "entry action after an exit action");

  OutputAction a;

  // The two kinds whose `line` is a timer index rather than an output line.
  // Bounds-checked against the timers, not against kMaxOutputLines: they are
  // different index spaces and the wrong check would let a set name a timer
  // that does not exist and silently do nothing at the moment it mattered.
  if (m.kind == statemachined_link_v1_ActionKind_ACTION_KIND_TIMER_START ||
      m.kind == statemachined_link_v1_ActionKind_ACTION_KIND_TIMER_CANCEL) {
    if (m.timer >= kMaxTimers) return fail(UploadError::BadField, "timer");
    a.output_line = static_cast<LineIndex>(m.timer);
    a.kind = m.kind == statemachined_link_v1_ActionKind_ACTION_KIND_TIMER_START
                 ? OutputActionKind::TimerStart
                 : OutputActionKind::TimerCancel;
  } else if (!parse_line_action(m, &a)) {
    return fail(UploadError::BadField, context_);
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
  fold(payload);
  return UploadError::None;
}

/// The action kinds whose `line` really is an output line. Split out only so
/// that add_action's timer branch and this one share one piece of bookkeeping
/// rather than each keeping its own copy of the entry/exit slice arithmetic --
/// two copies of that is how a state ends up owning another state's actions.
/// `context_` names what failed, since the caller reports it.
bool GraphBuilder::parse_line_action(const link::GraphAction& m, OutputAction* out) {
  OutputAction& a = *out;
  context_ = "line";
  if (m.line >= kMaxOutputLines) return false;
  a.output_line = static_cast<LineIndex>(m.line);

  context_ = "kind";
  switch (m.kind) {
    case statemachined_link_v1_ActionKind_ACTION_KIND_HIGH:
      a.kind = OutputActionKind::High;
      break;
    case statemachined_link_v1_ActionKind_ACTION_KIND_LOW:
      a.kind = OutputActionKind::Low;
      break;
    case statemachined_link_v1_ActionKind_ACTION_KIND_TOGGLE:
      a.kind = OutputActionKind::Toggle;
      break;
    case statemachined_link_v1_ActionKind_ACTION_KIND_PULSE:
      a.kind = OutputActionKind::Pulse;
      context_ = "pulse ms";
      if (m.ms == 0 || m.ms > UINT16_MAX) return false;
      a.pulse_ms = static_cast<NarrowMilliseconds>(m.ms);
      break;
    default:
      return false;
  }
  return true;
}

UploadError GraphBuilder::end_graph(const link::GraphEnd& m, link::PayloadSpan payload) {
  if (!graph_open_) return fail(UploadError::NotOpen, "graph_end");

  const uint32_t n_transitions = m.n_transitions;
  const uint32_t n_actions = m.n_output_actions;

  GraphEntry& e = g_.graphs[g_.n_graphs];
  if (e.n_states != expected_states_) return fail(UploadError::CountMismatch, "n_states");
  // Per graph, not per set: a host that miscounted one graph's transitions
  // should be told which graph.
  if (static_cast<uint32_t>(g_.n_transitions - graph_first_transition_) != n_transitions)
    return fail(UploadError::CountMismatch, "n_transitions");
  if (static_cast<uint32_t>(g_.n_output_actions - graph_first_action_) != n_actions)
    return fail(UploadError::CountMismatch, "n_output_actions");

  ++g_.n_graphs;
  graph_open_ = false;
  current_state_ = kNoState;
  fold(payload);
  return UploadError::None;
}

UploadError GraphBuilder::end_set(const link::SetEnd& m) {
  if (!open_) return fail(UploadError::NotOpen, "set_end");
  if (graph_open_) return fail(UploadError::BadOrder, "set_end before graph_end");
  if (g_.n_graphs != expected_graphs_) return fail(UploadError::CountMismatch, "n_graphs");

  const uint32_t n_states = m.n_states;
  const uint32_t n_transitions = m.n_transitions;
  const uint32_t n_actions = m.n_output_actions;

  if (g_.n_states != n_states) return fail(UploadError::CountMismatch, "n_states");
  if (g_.n_transitions != n_transitions)
    return fail(UploadError::CountMismatch, "n_transitions");
  if (g_.n_output_actions != n_actions)
    return fail(UploadError::CountMismatch, "n_output_actions");

  // The per-frame crc catches a corrupt message; this catches a missing one.
  if (m.checksum != checksum_) return fail(UploadError::ChecksumMismatch, "checksum");

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
