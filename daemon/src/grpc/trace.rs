// SPDX-License-Identifier: AGPL-3.0-or-later
//! What the rig is doing, and everything it said: the trace, on the wire.
//!
//! An entry is a JSON object with a `kind` and whatever that kind carries, and
//! **the set of kinds grows with the firmware** — so the payload crosses as a
//! `google.protobuf.Struct` rather than as a message per kind. Enumerating them
//! here would mean this daemon could not carry an entry from a board newer than
//! itself, which is the one thing a trace must always be able to do.
//!
//! Four keys are named on the message because every entry has them and a
//! reader sorts, joins and filters by them. The rest is payload.

use prost_types::value::Kind;
use serde_json::Value;

use crate::device::state_visit_trace::TraceEntry;
use crate::observer_registry::{unix_seconds_now, Observer};
use crate::wire::statemachined::v1 as wire;

/// The keys that become fields. Everything else in an entry is payload.
const NAMED_KEYS: [&str; 4] = ["entry_number", "kind", "recorded_host_time", "trial_id"];

pub fn trace_entry_to_wire(entry: &TraceEntry) -> wire::TraceEntry {
    let payload: prost_types::Struct = prost_types::Struct {
        fields: entry
            .iter()
            .filter(|(key, _)| !NAMED_KEYS.contains(&key.as_str()))
            .map(|(key, value)| (key.clone(), struct_value(value)))
            .collect(),
    };
    wire::TraceEntry {
        entry_number: entry
            .get("entry_number")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        kind: entry
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        recorded_host_time: entry
            .get("recorded_host_time")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        trial_id: entry.get("trial_id").and_then(Value::as_i64),
        payload: (!payload.fields.is_empty()).then_some(payload),
    }
}

/// One JSON value as a `Struct` value. Numbers are doubles there, as they are
/// in Python's `Struct`: the wire has no integer.
fn struct_value(value: &Value) -> prost_types::Value {
    let kind = match value {
        Value::Null => Kind::NullValue(0),
        Value::Bool(flag) => Kind::BoolValue(*flag),
        Value::Number(number) => Kind::NumberValue(number.as_f64().unwrap_or(0.0)),
        Value::String(text) => Kind::StringValue(text.clone()),
        Value::Array(items) => Kind::ListValue(prost_types::ListValue {
            values: items.iter().map(struct_value).collect(),
        }),
        Value::Object(fields) => Kind::StructValue(prost_types::Struct {
            fields: fields
                .iter()
                .map(|(k, v)| (k.clone(), struct_value(v)))
                .collect(),
        }),
    };
    prost_types::Value { kind: Some(kind) }
}

/// A window of the ring, and what fell out of it.
///
/// `lost_entries_before` is the only place the ring's boundedness is visible
/// from outside. Absent means nothing was lost; saying nothing at all would
/// hand back a shorter answer that looks complete.
pub fn trace_window_to_wire(
    entries: &[TraceEntry],
    newest_entry_number: i64,
    oldest_entry_number_still_held: i64,
    ring_capacity: usize,
    lost_entries_before: Option<i64>,
) -> wire::TraceWindow {
    wire::TraceWindow {
        entries: entries.iter().map(trace_entry_to_wire).collect(),
        newest_entry_number,
        oldest_entry_number_still_held,
        ring_capacity: ring_capacity as i32,
        lost_entries_before,
    }
}

pub fn observers_to_wire(observers: &[Observer]) -> wire::Observers {
    wire::Observers {
        observers: observers
            .iter()
            .map(|observer| wire::Observer {
                observer_id: observer.observer_id.to_string(),
                name: observer.name.clone(),
                stream: observer.stream.clone(),
                address: observer.address.clone().unwrap_or_default(),
                connected_at_unix_seconds: observer.connected_at,
                connected_seconds: (unix_seconds_now() - observer.connected_at).max(0.0),
                delivered: observer.delivered,
                fell_behind: observer.fell_behind,
            })
            .collect(),
        count: observers.len() as i32,
    }
}
