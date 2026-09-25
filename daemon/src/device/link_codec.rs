// SPDX-License-Identifier: AGPL-3.0-or-later
//! The board's messages, between the daemon's shape and protobuf.
//!
//! The daemon thinks in `{msg_type, message_id, ...fields}`: the compiler
//! writes upload messages that way, a result is reassembled from them, the
//! serial monitor shows them. That was the NDJSON wire's shape, and
//! `proto/statemachined/link/v1/link.proto` was written to keep it — a `oneof
//! body` member per `msg_type`, every field under its old member name. So this
//! is the only thing that had to learn protobuf, and it learns it from the
//! descriptor rather than from generated types: one mapping, driven by the
//! schema, instead of one hand-written per message that could drift.
//!
//! The mapping, both ways:
//!
//! * **A field with presence** — `optional`, a message, a `oneof` member — is
//!   left out when it is not set, and `null` sends it unset. So `"terminal":
//!   null` still means what it always did.
//! * **A plain field is always there**, zero included, as the NDJSON board
//!   always wrote it.
//! * **An enum value is its old token**: `ACTION_KIND_TIMER_START` is
//!   `"timer_start"`, the value's name with its enum's prefix taken off, in
//!   lower case. A number the schema does not name comes back as the number.
//! * **64-bit integers are JSON numbers.** A seed is a `u64` at last, not the
//!   hex string a double could not carry; serde_json holds it exactly.
//!
//! A field the schema does not have is **refused on the way out**. The board
//! would ignore it, which is exactly why it must not be sent: a field the
//! daemon thinks it set and the board never saw is a trial configured wrongly
//! and silently.

use std::sync::OnceLock;

use prost::Message;
use prost_reflect::{
    DescriptorPool, DynamicMessage, EnumDescriptor, FieldDescriptor, Kind, MessageDescriptor,
    ReflectMessage, Value as ProtoValue,
};
use serde_json::{Map, Value};

use super::message_framing::FramingError;
use crate::wire::LINK_DESCRIPTOR;

const HOST_MESSAGE: &str = "statemachined.link.v1.HostMessage";
const DEVICE_MESSAGE: &str = "statemachined.link.v1.DeviceMessage";

fn pool() -> &'static DescriptorPool {
    static POOL: OnceLock<DescriptorPool> = OnceLock::new();
    POOL.get_or_init(|| {
        DescriptorPool::decode(LINK_DESCRIPTOR).expect("the link descriptor protogen wrote")
    })
}

fn message_named(name: &str) -> MessageDescriptor {
    pool()
        .get_message_by_name(name)
        .unwrap_or_else(|| panic!("{name} is in link.proto"))
}

/// A command, as the protobuf a frame carries.
///
/// `msg_type` names the `oneof body` member; `fields` are that member's.
pub fn encode_host_message(
    msg_type: &str,
    message_id: u16,
    fields: &Map<String, Value>,
) -> Result<Vec<u8>, String> {
    encode_envelope(HOST_MESSAGE, msg_type, message_id, None, fields)
}

/// What a board would send: for the boards this daemon's tests stand in for,
/// and never on the daemon's own path.
pub fn encode_device_message(
    msg_type: &str,
    message_id: u16,
    in_reply_to: Option<u16>,
    fields: &Map<String, Value>,
) -> Result<Vec<u8>, String> {
    encode_envelope(DEVICE_MESSAGE, msg_type, message_id, in_reply_to, fields)
}

fn encode_envelope(
    envelope_name: &str,
    msg_type: &str,
    message_id: u16,
    in_reply_to: Option<u16>,
    fields: &Map<String, Value>,
) -> Result<Vec<u8>, String> {
    let envelope_descriptor = message_named(envelope_name);
    let body_field = envelope_descriptor
        .get_field_by_name(msg_type)
        .filter(|field| field.containing_oneof().is_some())
        .ok_or_else(|| {
            format!(
                "link.proto has no {} called {msg_type}",
                envelope_descriptor.name()
            )
        })?;
    let Kind::Message(body_descriptor) = body_field.kind() else {
        return Err(format!("{msg_type} is not a message in link.proto"));
    };
    let body = message_from_json(&body_descriptor, fields, msg_type)?;
    let mut envelope = DynamicMessage::new(envelope_descriptor);
    envelope.set_field_by_name("message_id", ProtoValue::U32(message_id as u32));
    if let Some(in_reply_to) = in_reply_to {
        envelope.set_field_by_name("in_reply_to", ProtoValue::U32(in_reply_to as u32));
    }
    envelope.set_field(&body_field, ProtoValue::Message(body));
    Ok(envelope.encode_to_vec())
}

/// A frame's protobuf from the board, as `{msg_type, message_id,
/// in_reply_to?, ...fields}`.
pub fn decode_device_message(payload: &[u8]) -> Result<Value, String> {
    decode_envelope(DEVICE_MESSAGE, payload)
}

/// A command's protobuf, in the same shape: what the serial monitor shows for
/// a frame that went the other way.
pub fn decode_host_message(payload: &[u8]) -> Result<Value, String> {
    decode_envelope(HOST_MESSAGE, payload)
}

fn decode_envelope(name: &str, payload: &[u8]) -> Result<Value, String> {
    let descriptor = message_named(name);
    let envelope = DynamicMessage::decode(descriptor.clone(), payload)
        .map_err(|problem| format!("not a {}: {problem}", descriptor.name()))?;
    let mut out = Map::new();
    let mut body = None;
    for field in descriptor.fields() {
        if field.containing_oneof().is_some() && envelope.has_field(&field) {
            body = Some(field);
        }
    }
    let Some(body_field) = body else {
        return Err(format!(
            "a {} with no body this daemon knows: a newer board's message",
            descriptor.name()
        ));
    };
    out.insert("msg_type".into(), Value::from(body_field.name()));
    out.insert(
        "message_id".into(),
        json_of(
            &envelope
                .get_field_by_name("message_id")
                .expect("an envelope field"),
            &Kind::Uint32,
        ),
    );
    if let Some(in_reply_to) = descriptor.get_field_by_name("in_reply_to") {
        if envelope.has_field(&in_reply_to) {
            out.insert(
                "in_reply_to".into(),
                json_of(&envelope.get_field(&in_reply_to), &in_reply_to.kind()),
            );
        }
    }
    if let ProtoValue::Message(body) = envelope.get_field(&body_field).as_ref() {
        for (key, value) in object_of(body) {
            out.insert(key, value);
        }
    }
    Ok(Value::Object(out))
}

/// What the serial monitor shows for a frame: the message it carried, as
/// compact JSON in the daemon's shape, or why it was not one.
///
/// The frame's own bytes are in hex when they could not be read, because that
/// is the case somebody opened a monitor to look at.
pub fn describe_frame(direction: &str, frame: &Result<Vec<u8>, FramingError>) -> String {
    let payload = match frame {
        Ok(payload) => payload,
        Err(problem) => return format!("<{problem}>"),
    };
    let decoded = if direction == super::serial_link::TO_DEVICE {
        decode_host_message(payload)
    } else {
        decode_device_message(payload)
    };
    match decoded {
        Ok(message) => message.to_string(),
        Err(problem) => {
            let hex: String = payload.iter().map(|byte| format!("{byte:02x}")).collect();
            format!("<bad_message: {problem}: {hex}>")
        }
    }
}

// -- JSON into protobuf -------------------------------------------------------

fn message_from_json(
    descriptor: &MessageDescriptor,
    fields: &Map<String, Value>,
    path: &str,
) -> Result<DynamicMessage, String> {
    let mut message = DynamicMessage::new(descriptor.clone());
    for (key, value) in fields {
        let where_ = format!("{path}.{key}");
        let Some(field) = descriptor.get_field_by_name(key) else {
            return Err(format!("{where_}: link.proto has no such field"));
        };
        if value.is_null() {
            // Unset, which for a field with presence is what null always meant.
            continue;
        }
        let converted = if field.is_list() {
            let Some(items) = value.as_array() else {
                return Err(format!("{where_}: expected a list, got {value}"));
            };
            ProtoValue::List(
                items
                    .iter()
                    .map(|item| scalar_from_json(&field, item, &where_))
                    .collect::<Result<_, _>>()?,
            )
        } else {
            scalar_from_json(&field, value, &where_)?
        };
        message.set_field(&field, converted);
    }
    Ok(message)
}

fn scalar_from_json(
    field: &FieldDescriptor,
    value: &Value,
    where_: &str,
) -> Result<ProtoValue, String> {
    let wrong = || format!("{where_}: {value} is not a {:?}", field.kind());
    let unsigned = |max: u64| value.as_u64().filter(|v| *v <= max).ok_or_else(wrong);
    let signed = || {
        value
            .as_i64()
            .filter(|v| i32::try_from(*v).is_ok())
            .ok_or_else(wrong)
    };
    Ok(match field.kind() {
        Kind::Uint32 | Kind::Fixed32 => ProtoValue::U32(unsigned(u32::MAX as u64)? as u32),
        Kind::Uint64 | Kind::Fixed64 => ProtoValue::U64(unsigned(u64::MAX)?),
        Kind::Int32 | Kind::Sint32 | Kind::Sfixed32 => ProtoValue::I32(signed()? as i32),
        Kind::Int64 | Kind::Sint64 | Kind::Sfixed64 => {
            ProtoValue::I64(value.as_i64().ok_or_else(wrong)?)
        }
        Kind::Bool => ProtoValue::Bool(value.as_bool().ok_or_else(wrong)?),
        Kind::String => ProtoValue::String(value.as_str().ok_or_else(wrong)?.to_string()),
        Kind::Enum(descriptor) => {
            let token = value.as_str().ok_or_else(wrong)?;
            let name = format!("{}{}", enum_prefix(&descriptor), token.to_ascii_uppercase());
            let named = descriptor
                .get_value_by_name(&name)
                .filter(|_| !token.is_empty() && token.to_ascii_lowercase() == token)
                .ok_or_else(|| {
                    format!(
                        "{where_}: {token:?} is not one of {}",
                        tokens_of(&descriptor)
                    )
                })?;
            ProtoValue::EnumNumber(named.number())
        }
        Kind::Message(descriptor) => {
            let Some(object) = value.as_object() else {
                return Err(wrong());
            };
            ProtoValue::Message(message_from_json(&descriptor, object, where_)?)
        }
        Kind::Float | Kind::Double | Kind::Bytes => {
            return Err(format!("{where_}: link.proto has no field of this kind"))
        }
    })
}

// -- protobuf into JSON -------------------------------------------------------

fn object_of(message: &DynamicMessage) -> Map<String, Value> {
    let mut out = Map::new();
    for field in message.descriptor().fields() {
        if field.supports_presence() && !message.has_field(&field) {
            continue;
        }
        let value = message.get_field(&field);
        let kind = field.kind();
        let json = match value.as_ref() {
            ProtoValue::List(items) => {
                Value::Array(items.iter().map(|item| json_of(item, &kind)).collect())
            }
            other => json_of(other, &kind),
        };
        out.insert(field.name().to_string(), json);
    }
    out
}

fn json_of(value: &ProtoValue, kind: &Kind) -> Value {
    match value {
        ProtoValue::Bool(v) => Value::from(*v),
        ProtoValue::I32(v) => Value::from(*v),
        ProtoValue::I64(v) => Value::from(*v),
        ProtoValue::U32(v) => Value::from(*v),
        ProtoValue::U64(v) => Value::from(*v),
        ProtoValue::String(v) => Value::from(v.as_str()),
        ProtoValue::EnumNumber(number) => match kind {
            Kind::Enum(descriptor) => descriptor
                .get_value(*number)
                .map(|named| {
                    Value::from(
                        named
                            .name()
                            .strip_prefix(&enum_prefix(descriptor))
                            .unwrap_or(named.name())
                            .to_ascii_lowercase(),
                    )
                })
                .unwrap_or_else(|| Value::from(*number)),
            _ => Value::from(*number),
        },
        ProtoValue::Message(message) => Value::Object(object_of(message)),
        ProtoValue::List(items) => {
            Value::Array(items.iter().map(|item| json_of(item, kind)).collect())
        }
        ProtoValue::F32(_) | ProtoValue::F64(_) | ProtoValue::Bytes(_) | ProtoValue::Map(_) => {
            Value::Null
        }
    }
}

/// `ActionKind` → `ACTION_KIND_`: the prefix every value of an enum carries,
/// because protobuf scopes enum values to the package and not to the enum.
fn enum_prefix(descriptor: &EnumDescriptor) -> String {
    let mut prefix = String::new();
    for (at, character) in descriptor.name().chars().enumerate() {
        if character.is_ascii_uppercase() && at > 0 {
            prefix.push('_');
        }
        prefix.push(character.to_ascii_uppercase());
    }
    prefix.push('_');
    prefix
}

fn tokens_of(descriptor: &EnumDescriptor) -> String {
    let prefix = enum_prefix(descriptor);
    descriptor
        .values()
        .filter(|value| value.number() != 0)
        .map(|value| {
            value
                .name()
                .strip_prefix(&prefix)
                .unwrap_or(value.name())
                .to_ascii_lowercase()
        })
        .collect::<Vec<_>>()
        .join(", ")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fields(value: Value) -> Map<String, Value> {
        value.as_object().unwrap().clone()
    }

    /// A command, decoded again from its own bytes.
    fn round_trip(msg_type: &str, body: Value) -> Value {
        let payload = encode_host_message(msg_type, 41, &fields(body)).unwrap();
        decode_host_message(&payload).unwrap()
    }

    #[test]
    fn a_command_crosses_under_its_old_names() {
        let back = round_trip(
            "graph_action",
            json!({"on": "entry", "line": 2, "kind": "timer_start", "timer": 1}),
        );
        assert_eq!(back["msg_type"], "graph_action");
        assert_eq!(back["message_id"], 41);
        assert_eq!(back["on"], "entry");
        assert_eq!(back["kind"], "timer_start");
        assert_eq!(back["timer"], 1);
        // A plain field is there even at zero, as the board always wrote it.
        assert_eq!(back["ms"], 0);
    }

    #[test]
    fn null_leaves_an_optional_field_unset_and_absence_stays_absence() {
        let back = round_trip(
            "graph_state",
            json!({"i": 0, "terminal": null, "timeout": {"dist": 0, "target": 1}}),
        );
        assert!(back.get("terminal").is_none());
        assert!(back.get("relight").is_none());
        assert_eq!(back["timeout"], json!({"dist": 0, "target": 1}));

        // And zero is not absence where presence is the point.
        let terminal_zero = round_trip("graph_state", json!({"i": 1, "terminal": 0}));
        assert_eq!(terminal_zero["terminal"], 0);
    }

    #[test]
    fn a_seed_is_a_whole_sixty_four_bit_number() {
        let back = round_trip("hello", json!({"proto": 2, "seed": u64::MAX - 1}));
        assert_eq!(back["seed"], json!(u64::MAX - 1));
    }

    #[test]
    fn negative_milliseconds_survive_as_signed() {
        let back = round_trip(
            "graph_dist",
            json!({"i": 0, "kind": "choice", "opts": [-5, 0, 700], "weights": [1, 2, 3]}),
        );
        assert_eq!(back["opts"], json!([-5, 0, 700]));
        assert_eq!(back["weights"], json!([1, 2, 3]));
        assert_eq!(back["kind"], "choice");
    }

    #[test]
    fn a_field_the_board_would_ignore_is_refused_instead_of_sent() {
        let refused =
            encode_host_message("configure", 1, &fields(json!({"trial_id": 1, "tirals": 2})))
                .unwrap_err();
        assert!(refused.contains("configure.tirals"), "{refused}");
    }

    #[test]
    fn a_token_the_schema_does_not_name_is_refused_with_the_ones_it_does() {
        let refused =
            encode_host_message("pins", 1, &fields(json!({"dir": "sideways"}))).unwrap_err();
        assert!(refused.contains("in, out"), "{refused}");
        let shouted = encode_host_message("pins", 1, &fields(json!({"dir": "IN"}))).unwrap_err();
        assert!(shouted.contains("\"IN\""), "{shouted}");
    }

    #[test]
    fn a_number_out_of_its_field_range_is_refused_not_wrapped() {
        assert!(encode_host_message("start", 1, &fields(json!({"trial_id": 1u64 << 32}))).is_err());
        assert!(encode_host_message("start", 1, &fields(json!({"trial_id": -1}))).is_err());
        assert!(encode_host_message("graph_dist", 1, &fields(json!({"a": 1i64 << 31}))).is_err());
    }

    #[test]
    fn a_command_link_proto_does_not_have_is_refused() {
        let refused = encode_host_message("teleport", 1, &Map::new()).unwrap_err();
        assert!(refused.contains("teleport"), "{refused}");
        // Nor may a device message be sent as a command.
        assert!(encode_host_message("pong", 1, &Map::new()).is_err());
    }

    #[test]
    fn a_device_message_names_what_it_answers_only_when_it_answers_something() {
        let descriptor = message_named(DEVICE_MESSAGE);
        let mut envelope = DynamicMessage::new(descriptor.clone());
        envelope.set_field_by_name("message_id", ProtoValue::U32(7));
        let visit_descriptor = pool()
            .get_message_by_name("statemachined.link.v1.Visit")
            .unwrap();
        let mut visit = DynamicMessage::new(visit_descriptor);
        visit.set_field_by_name("trial_id", ProtoValue::U32(193));
        envelope.set_field_by_name("visit", ProtoValue::Message(visit));
        let unasked = decode_device_message(&envelope.encode_to_vec()).unwrap();
        assert_eq!(unasked["msg_type"], "visit");
        assert!(unasked.get("in_reply_to").is_none());
        // A message field with nothing in it is absent, not an empty object.
        assert!(unasked.get("v").is_none());

        envelope.set_field_by_name("in_reply_to", ProtoValue::U32(0));
        let reply = decode_device_message(&envelope.encode_to_vec()).unwrap();
        assert_eq!(reply["in_reply_to"], 0);
    }

    #[test]
    fn a_device_message_with_no_body_this_daemon_knows_is_named_as_such() {
        // message_id 3, and field 99 -- a body from a newer board.
        let payload = [0x08, 0x03, 0x9A, 0x06, 0x00];
        let refused = decode_device_message(&payload).unwrap_err();
        assert!(refused.contains("newer board"), "{refused}");
    }

    #[test]
    fn every_enum_value_has_a_token_that_crosses_back() {
        for descriptor in pool().all_enums() {
            let prefix = enum_prefix(&descriptor);
            for value in descriptor.values() {
                assert!(
                    value.name().starts_with(&prefix),
                    "{} does not carry its enum's prefix {prefix}",
                    value.name()
                );
            }
        }
    }
}
