// SPDX-License-Identifier: GPL-3.0-or-later
// A JSON reader and writer for exactly the shapes docs/reference/protocol.md defines, and
// nothing else. No allocation, no floating point, one pass, and recursion
// bounded to kJsonMaxDepth -- four frames, which the protocol never exceeds and
// the parser refuses beyond.
//
// Why not ArduinoJson, which platformio.ini expected to arrive with this
// milestone: version 7 removed StaticJsonDocument and moved string storage to
// the heap, so the current library cannot satisfy this project's no-dynamic-
// allocation rule, and version 6 is no longer maintained. The protocol's
// messages are flat, ASCII, integer-only and at most four deep -- a scanner
// written to that shape is smaller than the dependency and its limits are
// visible here rather than in a changelog.
//
// The reader validates structure once, at construction, and indexes the
// top-level members. Every accessor afterwards is a lookup over that index, so
// a malformed document cannot be half-read: it is either valid or refused
// whole, which is the same promise the line layer below makes.
#pragma once
#include <cstddef>
#include <cstdint>

#include "config.h"

namespace statemachined {

/// A view into the message buffer. Nothing here copies: the reader borrows the
/// line for as long as the caller holds it, which on the device is until the
/// next line arrives.
struct JsonSpan {
  const char* p = nullptr;
  size_t n = 0;
};

enum class JsonType : uint8_t {
  Missing = 0,  ///< no such member
  Null,
  Bool,
  Number,
  String,  ///< the span excludes the quotes and is still escaped
  Object,
  Array,
};

enum class JsonError : uint8_t {
  None = 0,
  NotObject,     ///< the document does not open with '{'
  Malformed,     ///< a syntax error anywhere inside it
  TooDeep,       ///< nesting beyond kJsonMaxDepth
  TooManyKeys,   ///< more top-level members than kJsonMaxMembers
  DuplicateKey,  ///< the same top-level key twice
};

const char* json_error_str(JsonError e);

constexpr uint8_t kJsonMaxDepth = 4;     ///< docs/reference/protocol.md 1: a conforming
                                         ///< message never needs more
constexpr uint8_t kJsonMaxMembers = 16;  ///< the largest message this protocol
                                         ///< defines has ten

/// Compare a String span against a plain literal, decoding escapes as it goes
/// so that neither side needs a buffer. Our own senders never escape these
/// tokens; a host that does is still understood.
bool json_str_eq(JsonSpan s, const char* literal);

class JsonArray;

/// One JSON object, structurally validated and its top-level members indexed.
class JsonObject {
 public:
  JsonObject() = default;
  JsonObject(const char* text, size_t len) { parse(text, len); }

  /// Re-scan a nested object handed back by object(). Returns the error so a
  /// caller can answer bad_json with something specific in `context`.
  JsonError parse(const char* text, size_t len);

  bool valid() const { return err_ == JsonError::None; }
  JsonError error() const { return err_; }
  uint8_t size() const { return n_; }

  /// The raw span and type of a member, or Missing. Unknown members are the
  /// caller's to ignore -- that rule is how the protocol gains fields without a
  /// version bump.
  JsonType type_of(const char* key) const;
  JsonType raw(const char* key, JsonSpan* out) const;

  /// Typed reads. Each returns false if the member is absent or is not that
  /// type, so "absent" and "wrong" are one case at the call site: both mean the
  /// message does not say what the caller needs, and both answer bad_json.
  bool u32(const char* key, uint32_t* out) const;
  bool i32(const char* key, int32_t* out) const;
  bool u16(const char* key, uint16_t* out) const;
  bool u8(const char* key, uint8_t* out) const;
  bool i8(const char* key, int8_t* out) const;
  bool boolean(const char* key, bool* out) const;
  bool str(const char* key, JsonSpan* out) const;
  bool object(const char* key, JsonObject* out) const;
  bool array(const char* key, JsonArray* out) const;

  /// A 64-bit value carried as a hex string, because a JSON number through a
  /// double silently loses its low bits -- and a session seed that silently
  /// changes is a reproducibility bug nobody would ever find.
  bool hex64(const char* key, uint64_t* out) const;

  /// `null` is a value in this protocol, not an absence: a state's `terminal`
  /// is null when the state is not terminal, and that is different from a
  /// message that forgot to say.
  bool is_null(const char* key) const;

 private:
  JsonSpan keys_[kJsonMaxMembers];
  JsonSpan vals_[kJsonMaxMembers];
  JsonType types_[kJsonMaxMembers] = {};
  uint8_t n_ = 0;
  JsonError err_ = JsonError::NotObject;
};

/// Forward-only reader over an array of scalars. Arrays in this protocol carry
/// numbers or fixed-shape rows, never a mixture, so an index is not worth the
/// bytes it would cost to build.
class JsonArray {
 public:
  JsonArray() = default;
  JsonArray(const char* text, size_t len) : begin_(text), p_(text), end_(text + len) {
    rewind();
  }

  void rewind();
  /// False when the array is exhausted, or when the next element is not that
  /// type -- and in the second case the iteration ends there rather than
  /// skipping on. An element it cannot read means the sender and this reader
  /// disagree about the shape, and continuing would report a path that is not
  /// the one that happened.
  bool next_i32(int32_t* out);
  bool next_u32(uint32_t* out);
  bool next(JsonSpan* out, JsonType* type);

 private:
  const char* begin_ = nullptr;
  const char* p_ = nullptr;
  const char* end_ = nullptr;
};

/// Builds one message, in order, into a caller-owned buffer.
///
/// Overflow is sticky and silent until finish(), which then returns 0. A
/// message that does not fit is a firmware bug -- every message this protocol
/// defines is sized to kMaxLine by construction -- so the writer refuses rather
/// than emitting a truncated line that the far end would have to diagnose.
class JsonWriter {
 public:
  JsonWriter(char* buf, size_t cap) : buf_(buf), cap_(cap) {}

  /// `{"msg_type":"<name>","message_id":<id>` -- every message begins with
  /// these two. Pass msg_type_name(MsgType::...) rather than a literal.
  void begin(const char* msg_type, uint16_t message_id);
  /// The message_id of the command being answered. Unsolicited messages omit
  /// it, and so does a refusal of a line that never carried one.
  void in_reply_to(uint16_t message_id);

  void key_u32(const char* k, uint32_t v);
  void key_i32(const char* k, int32_t v);
  void key_bool(const char* k, bool v);
  void key_null(const char* k);
  void key_str(const char* k, const char* v);
  void key_hex64(const char* k, uint64_t v);

  void begin_array(const char* k);
  void end_array();
  void begin_object(const char* k);
  void end_object();

  /// Elements, inside an array.
  void elem_u32(uint32_t v);
  void elem_i32(int32_t v);
  void elem_str(const char* v);
  void begin_elem_array();

  bool overflowed() const { return overflow_; }
  size_t len() const { return len_; }

  /// Close the object, append the crc and the newline. 0 if anything
  /// overflowed on the way here.
  size_t finish(bool newline = true);

 private:
  void put(char c);
  void put_raw(const char* s);
  void put_escaped(const char* s);
  void put_u32(uint32_t v);
  void comma();
  void key(const char* k);

  char* buf_;
  size_t cap_;
  size_t len_ = 0;
  bool overflow_ = false;
  bool fresh_ = true;  ///< nothing written at this nesting level yet
};

}  // namespace statemachined
