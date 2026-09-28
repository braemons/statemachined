// SPDX-License-Identifier: GPL-3.0-or-later
#include "protocol/link_payload.h"

#include <pb_common.h>
#include <pb_decode.h>
#include <pb_encode.h>

namespace statemachined {
namespace link {
namespace {

void put_u16(uint8_t* p, uint16_t v) {
  p[0] = static_cast<uint8_t>(v & 0xFF);
  p[1] = static_cast<uint8_t>(v >> 8);
}

uint16_t get_u16(const uint8_t* p) { return static_cast<uint16_t>(p[0] | (p[1] << 8)); }

/// The body for `type`, found in the envelope's descriptor -- nanopb's own
/// table of which number is which message, generated from link.proto, so
/// there is no second one here to keep in step. `it.pData` is the union's
/// storage and `it.pSize` its `which_body`.
bool find_body(pb_field_iter_t* it, const pb_msgdesc_t* envelope, const void* message,
               pb_size_t type) {
  if (!pb_field_iter_begin_const(it, envelope, message)) return false;
  if (!pb_field_iter_find(it, type)) return false;
  return PB_HTYPE(it->type) == PB_HTYPE_ONEOF && PB_LTYPE_IS_SUBMSG(it->type);
}

template <typename Envelope>
PayloadError decode(const pb_msgdesc_t* envelope, const uint8_t* bytes, size_t n, Header* h,
                    Envelope* m, const char** why) {
  if (n < kHeaderBytes) {
    if (why != nullptr) *why = "shorter than a header";
    return PayloadError::TooShort;
  }
  h->type = bytes[0];
  const uint8_t flags = bytes[1];
  h->message_id = get_u16(bytes + 2);
  size_t at = kHeaderBytes;
  h->has_in_reply_to = (flags & kFlagInReplyTo) != 0;
  if (h->has_in_reply_to) {
    if (n < at + 2) {
      if (why != nullptr) *why = "shorter than its header";
      return PayloadError::TooShort;
    }
    h->in_reply_to = get_u16(bytes + at);
    at += 2;
  }
  pb_field_iter_t it;
  if (!find_body(&it, envelope, m, h->type)) {
    if (why != nullptr) *why = "unrecognised message type";
    return PayloadError::UnknownType;
  }
  *static_cast<pb_size_t*>(it.pSize) = h->type;
  pb_istream_t stream = pb_istream_from_buffer(bytes + at, n - at);
  if (!pb_decode_ex(&stream, it.submsg_desc, it.pData, PB_DECODE_NOINIT)) {
    if (why != nullptr) *why = PB_GET_ERROR(&stream);
    return PayloadError::BadBody;
  }
  return PayloadError::None;
}

size_t encode(const pb_msgdesc_t* envelope, const Header& h, const void* m, pb_size_t which,
              uint8_t* out, size_t cap) {
  pb_field_iter_t it;
  if (which > 0xFF || !find_body(&it, envelope, m, which)) return 0;
  const size_t at = kHeaderBytes + (h.has_in_reply_to ? 2 : 0);
  if (cap < at) return 0;
  out[0] = static_cast<uint8_t>(which);
  out[1] = h.has_in_reply_to ? kFlagInReplyTo : 0;
  put_u16(out + 2, h.message_id);
  if (h.has_in_reply_to) put_u16(out + kHeaderBytes, h.in_reply_to);
  pb_ostream_t stream = pb_ostream_from_buffer(out + at, cap - at);
  if (!pb_encode(&stream, it.submsg_desc, it.pData)) return 0;
  return at + stream.bytes_written;
}

}  // namespace

PayloadError decode_host_payload(const uint8_t* bytes, size_t n, Header* h, HostMessage* m,
                                 const char** why) {
  return decode(statemachined_link_v1_HostMessage_fields, bytes, n, h, m, why);
}

PayloadError decode_device_payload(const uint8_t* bytes, size_t n, Header* h, DeviceMessage* m,
                                   const char** why) {
  return decode(statemachined_link_v1_DeviceMessage_fields, bytes, n, h, m, why);
}

size_t encode_device_payload(const Header& h, const DeviceMessage& m, uint8_t* out,
                             size_t cap) {
  return encode(statemachined_link_v1_DeviceMessage_fields, h, &m, m.which_body, out, cap);
}

size_t encode_host_payload(const Header& h, const HostMessage& m, uint8_t* out, size_t cap) {
  return encode(statemachined_link_v1_HostMessage_fields, h, &m, m.which_body, out, cap);
}

}  // namespace link
}  // namespace statemachined
