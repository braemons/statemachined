#include "json.h"

#include "crc16.h"
#include "framing.h"

namespace fsmd {
namespace {

bool is_ws(char c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; }
bool is_digit(char c) { return c >= '0' && c <= '9'; }

const char* skip_ws(const char* p, const char* end) {
  while (p < end && is_ws(*p)) ++p;
  return p;
}

/// Past the closing quote, or nullptr. `inner` names the bytes between the
/// quotes, still escaped -- decoding needs a buffer and nothing here needs one.
const char* scan_string(const char* p, const char* end, JsonSpan* inner) {
  if (p >= end || *p != '"') return nullptr;
  const char* start = ++p;
  while (p < end) {
    const unsigned char c = static_cast<unsigned char>(*p);
    if (c == '"') {
      if (inner != nullptr) {
        inner->p = start;
        inner->n = static_cast<size_t>(p - start);
      }
      return p + 1;
    }
    if (c == '\\') {
      if (p + 1 >= end) return nullptr;
      const char e = p[1];
      if (e == 'u') {
        if (p + 5 >= end) return nullptr;
        for (int i = 2; i < 6; ++i) {
          const char h = p[i];
          if (!is_digit(h) && !(h >= 'a' && h <= 'f') && !(h >= 'A' && h <= 'F'))
            return nullptr;
        }
        p += 6;
        continue;
      }
      if (e != '"' && e != '\\' && e != '/' && e != 'b' && e != 'f' && e != 'n' && e != 'r' &&
          e != 't')
        return nullptr;
      p += 2;
      continue;
    }
    if (c < 0x20) return nullptr;  // a raw control byte is never a JSON string
    ++p;
  }
  return nullptr;
}

/// Integers only. A value with a '.' or an exponent is rejected rather than
/// truncated: this protocol has no floats, and a host sending 1.5 ms should be
/// told so rather than quietly given 1.
const char* scan_number(const char* p, const char* end) {
  const char* start = p;
  if (p < end && *p == '-') ++p;
  if (p >= end || !is_digit(*p)) return nullptr;
  if (*p == '0') {
    ++p;
  } else {
    while (p < end && is_digit(*p)) ++p;
  }
  if (p < end && (*p == '.' || *p == 'e' || *p == 'E')) return nullptr;
  return p == start ? nullptr : p;
}

const char* scan_literal(const char* p, const char* end, const char* word) {
  size_t i = 0;
  while (word[i] != '\0') {
    if (p + i >= end || p[i] != word[i]) return nullptr;
    ++i;
  }
  return p + i;
}

const char* scan_value(const char* p, const char* end, uint8_t depth, JsonType* type,
                       JsonSpan* span, JsonError* err);

/// One `{...}` or `[...]`. Shared because the two differ only in the closing
/// character and in whether each element is preceded by a key.
const char* scan_container(const char* p, const char* end, uint8_t depth, bool is_object,
                           JsonError* err) {
  const char close = is_object ? '}' : ']';
  ++p;  // the opening bracket
  p = skip_ws(p, end);
  if (p < end && *p == close) return p + 1;

  for (;;) {
    if (is_object) {
      p = scan_string(p, end, nullptr);
      if (p == nullptr) {
        *err = JsonError::Malformed;
        return nullptr;
      }
      p = skip_ws(p, end);
      if (p >= end || *p != ':') {
        *err = JsonError::Malformed;
        return nullptr;
      }
      p = skip_ws(p + 1, end);
    }
    p = scan_value(p, end, static_cast<uint8_t>(depth + 1), nullptr, nullptr, err);
    if (p == nullptr) return nullptr;
    p = skip_ws(p, end);
    if (p < end && *p == ',') {
      p = skip_ws(p + 1, end);
      continue;
    }
    if (p < end && *p == close) return p + 1;
    *err = JsonError::Malformed;
    return nullptr;
  }
}

const char* scan_value(const char* p, const char* end, uint8_t depth, JsonType* type,
                       JsonSpan* span, JsonError* err) {
  if (depth > kJsonMaxDepth) {
    *err = JsonError::TooDeep;
    return nullptr;
  }
  if (p >= end) {
    *err = JsonError::Malformed;
    return nullptr;
  }
  const char* start = p;
  JsonType t = JsonType::Missing;
  const char* after = nullptr;

  switch (*p) {
    case '"':
      t = JsonType::String;
      after = scan_string(p, end, span);
      if (after != nullptr && span != nullptr) {
        if (type != nullptr) *type = t;
        return after;  // span already names the bytes inside the quotes
      }
      break;
    case '{':
      t = JsonType::Object;
      after = scan_container(p, end, depth, true, err);
      break;
    case '[':
      t = JsonType::Array;
      after = scan_container(p, end, depth, false, err);
      break;
    case 't':
      t = JsonType::Bool;
      after = scan_literal(p, end, "true");
      break;
    case 'f':
      t = JsonType::Bool;
      after = scan_literal(p, end, "false");
      break;
    case 'n':
      t = JsonType::Null;
      after = scan_literal(p, end, "null");
      break;
    default:
      t = JsonType::Number;
      after = scan_number(p, end);
      break;
  }

  if (after == nullptr) {
    if (*err == JsonError::None) *err = JsonError::Malformed;
    return nullptr;
  }
  if (type != nullptr) *type = t;
  if (span != nullptr) {
    span->p = start;
    span->n = static_cast<size_t>(after - start);
  }
  return after;
}

bool spans_equal(JsonSpan a, JsonSpan b) {
  if (a.n != b.n) return false;
  for (size_t i = 0; i < a.n; ++i)
    if (a.p[i] != b.p[i]) return false;
  return true;
}

int hex_digit(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

/// Strict signed integer over the whole span. int64 so that the full u32 range
/// survives the parse and the width check happens once, at the accessor.
bool parse_int(JsonSpan s, int64_t* out) {
  if (s.n == 0) return false;
  size_t i = 0;
  bool neg = false;
  if (s.p[0] == '-') {
    neg = true;
    i = 1;
    if (s.n == 1) return false;
  }
  int64_t v = 0;
  for (; i < s.n; ++i) {
    if (!is_digit(s.p[i])) return false;
    v = v * 10 + (s.p[i] - '0');
    if (v > 0x100000000LL) return false;  // wider than anything on this wire
  }
  *out = neg ? -v : v;
  return true;
}

}  // namespace

const char* json_error_str(JsonError e) {
  switch (e) {
    case JsonError::None:
      return "ok";
    case JsonError::NotObject:
      return "not a JSON object";
    case JsonError::Malformed:
      return "malformed JSON";
    case JsonError::TooDeep:
      return "nested deeper than the protocol allows";
    case JsonError::TooManyKeys:
      return "more members than the protocol allows";
    case JsonError::DuplicateKey:
      return "the same member twice";
  }
  return "unknown";
}

bool json_str_eq(JsonSpan s, const char* literal) {
  size_t i = 0;
  const char* l = literal;
  while (i < s.n) {
    char c = s.p[i++];
    if (c == '\\' && i < s.n) {
      const char e = s.p[i++];
      switch (e) {
        case '"':
        case '\\':
        case '/':
          c = e;
          break;
        case 'b':
          c = '\b';
          break;
        case 'f':
          c = '\f';
          break;
        case 'n':
          c = '\n';
          break;
        case 'r':
          c = '\r';
          break;
        case 't':
          c = '\t';
          break;
        case 'u': {
          if (i + 4 > s.n) return false;
          int v = 0;
          for (int k = 0; k < 4; ++k) {
            const int d = hex_digit(s.p[i + k]);
            if (d < 0) return false;
            v = (v << 4) | d;
          }
          i += 4;
          // Anything above ASCII cannot match a literal in this codebase, and
          // decoding it would need a buffer to hold the UTF-8 it becomes.
          if (v == 0 || v > 0x7F) return false;
          c = static_cast<char>(v);
          break;
        }
        default:
          return false;
      }
    }
    if (*l == '\0' || *l != c) return false;
    ++l;
  }
  return *l == '\0';
}

JsonError JsonObject::parse(const char* text, size_t len) {
  n_ = 0;
  err_ = JsonError::None;

  const char* p = skip_ws(text, text + len);
  const char* const end = text + len;
  if (p >= end || *p != '{') {
    err_ = JsonError::NotObject;
    return err_;
  }
  p = skip_ws(p + 1, end);
  if (p < end && *p == '}') return err_;

  for (;;) {
    JsonSpan key;
    p = scan_string(p, end, &key);
    if (p == nullptr) {
      err_ = JsonError::Malformed;
      return err_;
    }
    p = skip_ws(p, end);
    if (p >= end || *p != ':') {
      err_ = JsonError::Malformed;
      return err_;
    }
    p = skip_ws(p + 1, end);

    JsonType type = JsonType::Missing;
    JsonSpan val;
    p = scan_value(p, end, 1, &type, &val, &err_);
    if (p == nullptr) {
      if (err_ == JsonError::None) err_ = JsonError::Malformed;
      return err_;
    }

    for (uint8_t i = 0; i < n_; ++i) {
      if (spans_equal(keys_[i], key)) {
        err_ = JsonError::DuplicateKey;
        return err_;
      }
    }
    if (n_ >= kJsonMaxMembers) {
      err_ = JsonError::TooManyKeys;
      return err_;
    }
    keys_[n_] = key;
    vals_[n_] = val;
    types_[n_] = type;
    ++n_;

    p = skip_ws(p, end);
    if (p < end && *p == ',') {
      p = skip_ws(p + 1, end);
      continue;
    }
    if (p < end && *p == '}') {
      p = skip_ws(p + 1, end);
      if (p != end) err_ = JsonError::Malformed;  // trailing rubbish
      return err_;
    }
    err_ = JsonError::Malformed;
    return err_;
  }
}

JsonType JsonObject::raw(const char* key, JsonSpan* out) const {
  if (!valid()) return JsonType::Missing;
  for (uint8_t i = 0; i < n_; ++i) {
    if (json_str_eq(keys_[i], key)) {
      if (out != nullptr) *out = vals_[i];
      return types_[i];
    }
  }
  return JsonType::Missing;
}

JsonType JsonObject::type_of(const char* key) const { return raw(key, nullptr); }

bool JsonObject::is_null(const char* key) const { return raw(key, nullptr) == JsonType::Null; }

bool JsonObject::i32(const char* key, int32_t* out) const {
  JsonSpan s;
  if (raw(key, &s) != JsonType::Number) return false;
  int64_t v = 0;
  if (!parse_int(s, &v)) return false;
  if (v < INT32_MIN || v > INT32_MAX) return false;
  *out = static_cast<int32_t>(v);
  return true;
}

bool JsonObject::u32(const char* key, uint32_t* out) const {
  JsonSpan s;
  if (raw(key, &s) != JsonType::Number) return false;
  int64_t v = 0;
  if (!parse_int(s, &v)) return false;
  if (v < 0 || v > UINT32_MAX) return false;
  *out = static_cast<uint32_t>(v);
  return true;
}

bool JsonObject::u16(const char* key, uint16_t* out) const {
  uint32_t v = 0;
  if (!u32(key, &v) || v > UINT16_MAX) return false;
  *out = static_cast<uint16_t>(v);
  return true;
}

bool JsonObject::u8(const char* key, uint8_t* out) const {
  uint32_t v = 0;
  if (!u32(key, &v) || v > UINT8_MAX) return false;
  *out = static_cast<uint8_t>(v);
  return true;
}

bool JsonObject::i8(const char* key, int8_t* out) const {
  int32_t v = 0;
  if (!i32(key, &v) || v < INT8_MIN || v > INT8_MAX) return false;
  *out = static_cast<int8_t>(v);
  return true;
}

bool JsonObject::boolean(const char* key, bool* out) const {
  JsonSpan s;
  if (raw(key, &s) != JsonType::Bool) return false;
  *out = (s.n == 4);  // "true" against "false"
  return true;
}

bool JsonObject::str(const char* key, JsonSpan* out) const {
  return raw(key, out) == JsonType::String;
}

bool JsonObject::object(const char* key, JsonObject* out) const {
  JsonSpan s;
  if (raw(key, &s) != JsonType::Object) return false;
  return out->parse(s.p, s.n) == JsonError::None;
}

bool JsonObject::array(const char* key, JsonArray* out) const {
  JsonSpan s;
  if (raw(key, &s) != JsonType::Array) return false;
  *out = JsonArray(s.p, s.n);
  return true;
}

bool JsonObject::hex64(const char* key, uint64_t* out) const {
  JsonSpan s;
  if (raw(key, &s) != JsonType::String) return false;
  if (s.n == 0 || s.n > 16) return false;
  uint64_t v = 0;
  for (size_t i = 0; i < s.n; ++i) {
    const int d = hex_digit(s.p[i]);
    if (d < 0) return false;
    v = (v << 4) | static_cast<uint64_t>(d);
  }
  *out = v;
  return true;
}

void JsonArray::rewind() {
  if (begin_ == nullptr) return;
  const char* q = skip_ws(begin_, end_);
  if (q < end_ && *q == '[') ++q;
  p_ = q;
}

bool JsonArray::next(JsonSpan* out, JsonType* type) {
  if (p_ == nullptr) return false;
  const char* q = skip_ws(p_, end_);
  if (q < end_ && *q == ',') q = skip_ws(q + 1, end_);
  if (q >= end_ || *q == ']') {
    p_ = nullptr;
    return false;
  }
  JsonError err = JsonError::None;
  JsonType t = JsonType::Missing;
  JsonSpan s;
  const char* after = scan_value(q, end_, 2, &t, &s, &err);
  if (after == nullptr) {
    p_ = nullptr;
    return false;
  }
  p_ = after;
  if (out != nullptr) *out = s;
  if (type != nullptr) *type = t;
  return true;
}

bool JsonArray::next_i32(int32_t* out) {
  JsonSpan s;
  JsonType t = JsonType::Missing;
  if (!next(&s, &t) || t != JsonType::Number) {
    p_ = nullptr;
    return false;
  }
  int64_t v = 0;
  if (!parse_int(s, &v) || v < INT32_MIN || v > INT32_MAX) {
    p_ = nullptr;
    return false;
  }
  *out = static_cast<int32_t>(v);
  return true;
}

bool JsonArray::next_u32(uint32_t* out) {
  JsonSpan s;
  JsonType t = JsonType::Missing;
  if (!next(&s, &t) || t != JsonType::Number) {
    p_ = nullptr;
    return false;
  }
  int64_t v = 0;
  if (!parse_int(s, &v) || v < 0 || v > UINT32_MAX) {
    p_ = nullptr;
    return false;
  }
  *out = static_cast<uint32_t>(v);
  return true;
}

// ---------------------------------------------------------------- writer ---

void JsonWriter::put(char c) {
  if (overflow_) return;
  if (len_ >= cap_) {
    overflow_ = true;
    return;
  }
  buf_[len_++] = c;
}

void JsonWriter::put_raw(const char* s) {
  for (; *s != '\0'; ++s) put(*s);
}

void JsonWriter::put_escaped(const char* s) {
  static const char kHex[] = "0123456789ABCDEF";
  for (; *s != '\0'; ++s) {
    const unsigned char c = static_cast<unsigned char>(*s);
    if (c == '"' || c == '\\') {
      put('\\');
      put(static_cast<char>(c));
    } else if (c < 0x20 || c >= 0x80) {
      // Control bytes must be escaped and non-ASCII may not cross this wire at
      // all -- \uXXXX satisfies both without a second code path.
      put_raw("\\u00");
      put(kHex[(c >> 4) & 0x0F]);
      put(kHex[c & 0x0F]);
    } else {
      put(static_cast<char>(c));
    }
  }
}

void JsonWriter::put_u32(uint32_t v) {
  char tmp[10];
  uint8_t n = 0;
  do {
    tmp[n++] = static_cast<char>('0' + (v % 10));
    v /= 10;
  } while (v != 0);
  while (n > 0) put(tmp[--n]);
}

void JsonWriter::comma() {
  if (!fresh_) put(',');
  fresh_ = false;
}

void JsonWriter::key(const char* k) {
  comma();
  put('"');
  put_raw(k);
  put_raw("\":");
}

void JsonWriter::begin(const char* type, uint16_t seq) {
  put('{');
  fresh_ = true;
  key_str("t", type);
  key_u32("seq", seq);
}

void JsonWriter::req(uint16_t seq) { key_u32("req", seq); }

void JsonWriter::key_u32(const char* k, uint32_t v) {
  key(k);
  put_u32(v);
}

void JsonWriter::key_i32(const char* k, int32_t v) {
  key(k);
  if (v < 0) {
    put('-');
    put_u32(static_cast<uint32_t>(-static_cast<int64_t>(v)));
  } else {
    put_u32(static_cast<uint32_t>(v));
  }
}

void JsonWriter::key_bool(const char* k, bool v) {
  key(k);
  put_raw(v ? "true" : "false");
}

void JsonWriter::key_null(const char* k) {
  key(k);
  put_raw("null");
}

void JsonWriter::key_str(const char* k, const char* v) {
  key(k);
  put('"');
  put_escaped(v);
  put('"');
}

void JsonWriter::key_hex64(const char* k, uint64_t v) {
  static const char kHex[] = "0123456789ABCDEF";
  key(k);
  put('"');
  bool started = false;
  for (int shift = 60; shift >= 0; shift -= 4) {
    const char c = kHex[(v >> shift) & 0x0F];
    if (!started && c == '0' && shift != 0) continue;
    started = true;
    put(c);
  }
  put('"');
}

void JsonWriter::begin_array(const char* k) {
  key(k);
  put('[');
  fresh_ = true;
}

void JsonWriter::end_array() {
  put(']');
  fresh_ = false;
}

void JsonWriter::begin_object(const char* k) {
  key(k);
  put('{');
  fresh_ = true;
}

void JsonWriter::end_object() {
  put('}');
  fresh_ = false;
}

void JsonWriter::elem_u32(uint32_t v) {
  comma();
  put_u32(v);
}

void JsonWriter::elem_i32(int32_t v) {
  comma();
  if (v < 0) {
    put('-');
    put_u32(static_cast<uint32_t>(-static_cast<int64_t>(v)));
  } else {
    put_u32(static_cast<uint32_t>(v));
  }
}

void JsonWriter::elem_str(const char* v) {
  comma();
  put('"');
  put_escaped(v);
  put('"');
}

void JsonWriter::begin_elem_array() {
  comma();
  put('[');
  fresh_ = true;
}

size_t JsonWriter::finish(bool newline) {
  if (overflow_) return 0;
  return finish_frame(buf_, len_, cap_, newline);
}

}  // namespace fsmd
