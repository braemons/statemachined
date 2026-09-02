// Assembles a StateGraph from the upload messages, one message at a time.
//
// The upload is chunked because the device has 32 KB and a whole graph as JSON
// does not fit -- peak parse buffer is one message, never the document. See
// dev/PROTOCOL.md 3.2.
//
// Two invariants live here rather than in the protocol document alone:
//
//   * The staged graph is separate from the committed one. A failed or
//     abandoned upload leaves the device running the paradigm it was already
//     running, which is what makes it safe to re-upload mid-session.
//
//   * A state owns its transitions and its actions as a (first, count) slice of
//     one flat pool. A slice is contiguous only if everything belonging to a
//     state arrives together, so the wire's ordering rule *is* the memory
//     invariant -- and a violation is refused rather than producing a state
//     that owns somebody else's transitions.
#pragma once
#include <cstdint>

#include "config.h"
#include "graph/state_graph.h"
#include "protocol/json.h"

namespace fsmd {

enum class UploadError : uint8_t {
  None = 0,
  NotOpen,           ///< a graph message before graph_begin
  BadOrder,          ///< out of the order 3.2 requires
  BadIndex,          ///< an `i` that did not match the count already accepted
  TooMany,           ///< a capacity exceeded; context() names which one
  BadField,          ///< a member missing, of the wrong type, or out of range
  CountMismatch,     ///< graph_end's totals disagree with what arrived
  ChecksumMismatch,  ///< a message went missing between begin and end
  Invalid,           ///< the assembled graph failed validate()
};

const char* upload_error_str(UploadError e);

/// Stages one graph upload.
class GraphBuilder {
 public:
  /// Open an upload, discarding any half-finished one. `covered` is the
  /// CRC-covered prefix of the line, which is what the running checksum folds
  /// in -- see dev/PROTOCOL.md 1.1.
  UploadError begin(const JsonObject& m, JsonSpan covered);

  UploadError add_distribution(const JsonObject& m, JsonSpan covered);
  UploadError add_state(const JsonObject& m, JsonSpan covered);
  UploadError add_transition(const JsonObject& m, JsonSpan covered);
  UploadError add_action(const JsonObject& m, JsonSpan covered);

  /// Check the totals and the checksum, then validate. `graph_end` is not
  /// folded into the checksum -- it carries it.
  UploadError end(const JsonObject& m);

  /// Throw away a partial upload: a `hello`, or a second `graph_begin`.
  void abandon() { open_ = false; }

  bool open() const { return open_; }

  /// True once end() has returned None. The staged graph is then a validated
  /// graph the caller may commit.
  bool complete() const { return complete_; }

  const StateGraph& staged() const { return g_; }

  /// What specifically failed -- the capacity that overflowed, the member that
  /// was missing. Never empty after an error: "every refusal names what to
  /// change", so an empty context is a defect here rather than a terse style.
  const char* context() const { return context_; }

  /// Set when end() returned Invalid, so the caller can report which rule the
  /// assembled graph broke rather than just that it broke one.
  GraphError graph_error() const { return graph_error_; }

 private:
  UploadError fail(UploadError e, const char* what);
  void fold(JsonSpan covered);

  StateGraph g_;
  uint16_t checksum_ = 0xFFFF;
  uint8_t expected_states_ = 0;          ///< what graph_begin declared
  StateIndex current_state_ = kNoState;  ///< what transitions and actions attach to
  bool seen_exit_action_ = false;        ///< entry actions may no longer arrive
  bool open_ = false;
  bool complete_ = false;
  GraphError graph_error_ = GraphError::None;
  const char* context_ = "";
};

}  // namespace fsmd
