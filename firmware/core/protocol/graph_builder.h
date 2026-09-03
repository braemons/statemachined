// SPDX-License-Identifier: GPL-3.0-or-later
// Assembles a GraphSet from the upload messages, one message at a time.
//
// The upload is chunked because the device has 32 KB and a whole set as JSON
// does not fit -- peak parse buffer is one message, never the document. See
// dev/PROTOCOL.md 3.2.
//
// Three invariants live here rather than in the protocol document alone:
//
//   * It builds **in place**, into the set the session runs from. Two sets do
//     not fit on a 32 KB board, so the double buffering a single graph had is
//     gone, and with it the guarantee that a failed upload left the previous
//     paradigm running. What replaces it is failing loudly: a set is invalid
//     from set_begin until set_end succeeds, so a board part-way through a
//     failed upload holds NO graph and every output sits at its safe level.
//     That is a worse outcome than the old one and a much more visible one,
//     and it can only happen between sessions -- an upload is refused while a
//     trial is armed.
//
//   * A state owns its transitions and its actions as a (first, count) slice of
//     one flat pool, and a graph owns its states the same way. A slice is
//     contiguous only if everything belonging to it arrives together, so the
//     wire's ordering rule *is* the memory invariant -- and a violation is
//     refused rather than producing a state that owns somebody else's
//     transitions.
//
//   * State indices on the wire are **per graph**, counted from zero, because
//     that is how the host authored them. Every other pool index -- a
//     distribution, in particular -- is set-global, because those pools are
//     genuinely shared and a graph reusing another's distribution is a feature.
//     The translation happens here and nowhere else.
#pragma once
#include <cstdint>

#include "config.h"
#include "graph/graph_set.h"
#include "protocol/json.h"

namespace statemachined {

enum class UploadError : uint8_t {
  None = 0,
  NotOpen,           ///< a graph message before graph_begin
  BadOrder,          ///< out of the order 3.2 requires
  BadIndex,          ///< an `i` that did not match the count already accepted
  TooMany,           ///< a capacity exceeded; context() names which one
  BadField,          ///< a member missing, of the wrong type, or out of range
  CountMismatch,     ///< an end message's totals disagree with what arrived
  ChecksumMismatch,  ///< a message went missing between begin and end
  Invalid,           ///< the assembled set failed validate()
};

const char* upload_error_str(UploadError e);

/// Assembles one set upload, in place.
class GraphBuilder {
 public:
  /// `target` is the set this fills, and it is the live one. See the note at
  /// the top of this file about what that costs and what it buys.
  explicit GraphBuilder(GraphSet& target) : g_(target) {}

  /// Open a set upload, discarding whatever the target held. `covered` is the
  /// CRC-covered prefix of the line, which is what the running checksum folds
  /// in -- see dev/PROTOCOL.md 1.1.
  UploadError begin_set(const JsonObject& m, JsonSpan covered);

  /// Open one graph within the set. Its `slot` must be the next one: a set
  /// arrives in order, so that a graph's states are a contiguous slice.
  UploadError begin_graph(const JsonObject& m, JsonSpan covered);

  UploadError add_distribution(const JsonObject& m, JsonSpan covered);
  UploadError add_state(const JsonObject& m, JsonSpan covered);
  UploadError add_transition(const JsonObject& m, JsonSpan covered);
  UploadError add_action(const JsonObject& m, JsonSpan covered);

  /// Close one graph and check its own totals.
  UploadError end_graph(const JsonObject& m, JsonSpan covered);

  /// Check the set's totals and the checksum, then validate the whole set.
  /// `set_end` is not folded into the checksum -- it carries it.
  UploadError end_set(const JsonObject& m);

  /// Throw away a partial upload: a `hello`, or a second `set_begin`. The
  /// target is left invalid, which is the point.
  void abandon() {
    open_ = false;
    graph_open_ = false;
    complete_ = false;
  }

  bool open() const { return open_; }

  /// True once end_set() has returned None. The target is then a validated set
  /// the caller may run from.
  bool complete() const { return complete_; }

  /// What specifically failed -- the capacity that overflowed, the member that
  /// was missing. Never empty after an error: "every refusal names what to
  /// change", so an empty context is a defect here rather than a terse style.
  const char* context() const { return context_; }

  /// Set when end_set() returned Invalid, so the caller can report which rule
  /// the assembled set broke rather than just that it broke one.
  GraphError graph_error() const { return graph_error_; }

 private:
  UploadError fail(UploadError e, const char* what);
  void fold(JsonSpan covered);

  GraphSet& g_;
  uint16_t checksum_ = 0xFFFF;
  uint8_t expected_graphs_ = 0;          ///< what set_begin declared
  uint8_t expected_states_ = 0;          ///< what the current graph_begin declared
  StateIndex first_state_ = 0;           ///< where the current graph's slice starts
  StateIndex current_state_ = kNoState;  ///< absolute; what transitions and actions attach to
  uint8_t graph_first_transition_ = 0;   ///< for graph_end's per-graph totals
  uint8_t graph_first_action_ = 0;
  bool seen_exit_action_ = false;  ///< entry actions may no longer arrive
  bool open_ = false;
  bool graph_open_ = false;
  bool complete_ = false;
  GraphError graph_error_ = GraphError::None;
  const char* context_ = "";
};

}  // namespace statemachined
