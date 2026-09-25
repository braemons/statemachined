// SPDX-License-Identifier: AGPL-3.0-or-later
//! Who is watching this rig, while they are watching it.
//!
//! **This daemon reports to nobody.** It publishes what it did and assumes
//! nobody read it. So an observer is not a subscriber this daemon serves: it is
//! a *fact about right now* — something opened a stream, said what it was, and
//! has not gone away. Nothing here can hold up a trial, nothing is retried,
//! nothing is remembered across a restart, and closing the stream is the whole
//! of unregistering.
//!
//! **Why keep the list at all**: because "is triald actually listening?" is
//! the first question anybody asks when trials stop being recorded, and without
//! this the answer is a packet capture.
//!
//! A name is whatever the observer called itself. It is **self-declared and
//! unverified** — nothing is granted by it, so a wrong name is a wrong label on
//! a diagnostic screen and nothing more.

use std::collections::BTreeMap;
use std::sync::Mutex;

/// What an observer is called when it does not say. Not an error: a browser
/// tab is an observer too, and it has nothing useful to declare.
pub const UNNAMED: &str = "unnamed";

/// The longest a self-declared name may be before it is cut. A display limit
/// rather than a security one — there is nothing here to protect.
pub const NAME_LIMIT: usize = 64;

/// One live stream, and what it has had from this daemon.
#[derive(Debug, Clone, PartialEq)]
pub struct Observer {
    pub observer_id: u64,
    pub name: String,
    /// Which stream: `state` (coalesced) or `trace` (lossless).
    pub stream: String,
    /// The peer's address as the server saw it, when there was one.
    pub address: Option<String>,
    /// Unix seconds, so a panel can say how long it has been watching.
    pub connected_at: f64,
    /// Messages sent to this observer. A counter that does not move is the tell.
    pub delivered: i64,
    /// The trace ring passed this one by.
    pub fell_behind: bool,
}

#[derive(Default)]
struct Registered {
    observers: BTreeMap<u64, Observer>,
    last_id: u64,
}

/// The live streams, and nothing about the ones that have gone.
#[derive(Default)]
pub struct ObserverRegistry {
    registered: Mutex<Registered>,
}

impl ObserverRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    fn registered(&self) -> std::sync::MutexGuard<'_, Registered> {
        self.registered
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Take note of a stream that has just opened. Gives its id, from 1.
    pub fn register(&self, name: Option<&str>, stream: &str, address: Option<String>) -> u64 {
        let mut registered = self.registered();
        registered.last_id += 1;
        let observer_id = registered.last_id;
        registered.observers.insert(
            observer_id,
            Observer {
                observer_id,
                name: clean_name(name),
                stream: stream.to_string(),
                address,
                connected_at: unix_seconds_now(),
                delivered: 0,
                fell_behind: false,
            },
        );
        observer_id
    }

    /// Forget it. Called when a stream ends however it ends, so it must not
    /// care if it is gone.
    pub fn unregister(&self, observer_id: u64) {
        self.registered().observers.remove(&observer_id);
    }

    /// Count what an observer has been sent, so a stalled one is visible.
    pub fn note_delivery(&self, observer_id: u64, messages: i64) {
        if messages <= 0 {
            return;
        }
        if let Some(observer) = self.registered().observers.get_mut(&observer_id) {
            observer.delivered += messages;
        }
    }

    /// It lost entries out of the ring.
    pub fn note_fell_behind(&self, observer_id: u64) {
        if let Some(observer) = self.registered().observers.get_mut(&observer_id) {
            observer.fell_behind = true;
        }
    }

    /// Everything watching now, oldest connection first.
    pub fn observers(&self) -> Vec<Observer> {
        let mut observers: Vec<Observer> = self.registered().observers.values().cloned().collect();
        // Stable, so two that connected in the same instant stay in id order,
        // as Python's `sorted` keeps them in insertion order.
        observers.sort_by(|a, b| a.connected_at.total_cmp(&b.connected_at));
        observers
    }

    pub fn len(&self) -> usize {
        self.registered().observers.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// A self-declared name, made safe to put on a screen.
///
/// Control characters out and length capped: this string arrives off the wire
/// and lands in a panel, and nothing has a reason to render an observer's
/// newlines.
pub fn clean_name(name: Option<&str>) -> String {
    let Some(name) = name.filter(|name| !name.is_empty()) else {
        return UNNAMED.to_string();
    };
    let printable: String = name.chars().filter(|c| is_printable(*c)).collect();
    let trimmed: String = printable.trim().chars().take(NAME_LIMIT).collect();
    if trimmed.is_empty() {
        UNNAMED.to_string()
    } else {
        trimmed
    }
}

/// Python's `str.isprintable` for one character, near enough to agree on
/// everything a client would send: controls, and separators other than the
/// plain space, are not printable.
fn is_printable(c: char) -> bool {
    if c == ' ' {
        return true;
    }
    !(c.is_control()
        || c.is_whitespace()
        || matches!(c, '\u{200b}'..='\u{200f}' | '\u{2028}'..='\u{202e}' | '\u{2060}'..='\u{2064}' | '\u{feff}'))
}

pub fn unix_seconds_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("a clock after 1970")
        .as_secs_f64()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn a_registry_with(names: &[&str]) -> (ObserverRegistry, Vec<u64>) {
        let registry = ObserverRegistry::new();
        let ids = names
            .iter()
            .map(|name| registry.register(Some(name), "trace", Some("127.0.0.1".into())))
            .collect();
        (registry, ids)
    }

    #[test]
    fn an_observer_is_listed_while_it_is_connected() {
        let (registry, ids) = a_registry_with(&["triald"]);
        assert_eq!(registry.observers()[0].name, "triald");
        assert_eq!(registry.observers()[0].stream, "trace");
        registry.unregister(ids[0]);
        assert!(registry.is_empty());
    }

    #[test]
    fn unregistering_twice_is_fine() {
        let (registry, ids) = a_registry_with(&["triald"]);
        registry.unregister(ids[0]);
        registry.unregister(ids[0]);
        assert!(registry.is_empty());
    }

    #[test]
    fn deliveries_are_counted_so_a_stalled_observer_is_visible() {
        let (registry, ids) = a_registry_with(&["triald"]);
        registry.note_delivery(ids[0], 1);
        registry.note_delivery(ids[0], 3);
        assert_eq!(registry.observers()[0].delivered, 4);
    }

    #[test]
    fn a_delivery_of_nothing_does_not_move_the_counter() {
        let (registry, ids) = a_registry_with(&["triald"]);
        registry.note_delivery(ids[0], 0);
        assert_eq!(registry.observers()[0].delivered, 0);
    }

    #[test]
    fn an_observer_that_lost_entries_says_so() {
        let (registry, ids) = a_registry_with(&["triald"]);
        registry.note_fell_behind(ids[0]);
        assert!(registry.observers()[0].fell_behind);
    }

    #[test]
    fn counting_an_observer_that_has_gone_is_not_an_error() {
        let (registry, ids) = a_registry_with(&["triald"]);
        registry.unregister(ids[0]);
        registry.note_delivery(ids[0], 1);
        registry.note_fell_behind(ids[0]);
        assert!(registry.is_empty());
    }

    #[test]
    fn observers_are_listed_oldest_first() {
        let (registry, _) = a_registry_with(&["triald", "console", "a browser tab"]);
        let names: Vec<String> = registry.observers().into_iter().map(|o| o.name).collect();
        assert_eq!(names, ["triald", "console", "a browser tab"]);
    }

    #[test]
    fn a_name_is_self_declared_and_nothing_is_granted_by_it() {
        assert_eq!(clean_name(Some("triald")), "triald");
        assert_eq!(clean_name(None), UNNAMED);
        assert_eq!(clean_name(Some("")), UNNAMED);
        assert_eq!(clean_name(Some("   ")), UNNAMED);
    }

    #[test]
    fn a_name_cannot_carry_control_characters_onto_a_screen() {
        assert_eq!(clean_name(Some("tri\nald\t")), "triald");
        assert_eq!(clean_name(Some(&"x".repeat(200))), "x".repeat(64));
    }
}
