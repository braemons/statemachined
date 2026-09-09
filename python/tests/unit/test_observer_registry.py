# SPDX-License-Identifier: GPL-3.0-or-later
"""Who is watching, and why the daemon keeps a list it never reads."""

from __future__ import annotations

from statemachined.daemon.observer_registry import UNNAMED, ObserverRegistry, clean_name


def a_registry_with(*names: str) -> tuple[ObserverRegistry, list[int]]:
    registry = ObserverRegistry()
    ids = [
        registry.register(name=name, stream="trace", address=f"10.0.0.{i}:5000")
        for i, name in enumerate(names, start=1)
    ]
    return registry, ids


def test_an_observer_is_listed_while_it_is_connected():
    registry, [observer_id] = a_registry_with("triald")
    listed = registry.observers()
    assert [o.name for o in listed] == ["triald"]
    assert listed[0].address == "10.0.0.1:5000"
    assert listed[0].delivered == 0

    # Closing the socket is the whole of unsubscribing. Nothing is remembered.
    registry.unregister(observer_id)
    assert registry.observers() == []


def test_unregistering_twice_is_fine():
    # It is called from a `finally`, which runs on paths where the registration
    # never happened or has already been cleaned up.
    registry, [observer_id] = a_registry_with("triald")
    registry.unregister(observer_id)
    registry.unregister(observer_id)
    assert len(registry) == 0


def test_deliveries_are_counted_so_a_stalled_observer_is_visible():
    # The whole diagnostic value: a connection that is up and a counter that
    # does not move is a different fault from no connection at all.
    registry, [observer_id] = a_registry_with("triald")
    registry.note_delivery(observer_id, 12)
    registry.note_delivery(observer_id)
    assert registry.observers()[0].delivered == 13


def test_a_delivery_of_nothing_does_not_move_the_counter():
    # The trace stream calls this with however many entries it just sent, and
    # most of the time that is none.
    registry, [observer_id] = a_registry_with("triald")
    registry.note_delivery(observer_id, 0)
    assert registry.observers()[0].delivered == 0


def test_an_observer_that_lost_entries_says_so():
    registry, [observer_id] = a_registry_with("triald")
    registry.note_fell_behind(observer_id)
    assert registry.observers()[0].fell_behind is True


def test_counting_an_observer_that_has_gone_is_not_an_error():
    # The link thread and the socket's own coroutine are different threads, so
    # a delivery can be counted for a connection that has just closed.
    registry, [observer_id] = a_registry_with("triald")
    registry.unregister(observer_id)
    registry.note_delivery(observer_id)
    registry.note_fell_behind(observer_id)
    assert registry.observers() == []


def test_observers_are_listed_oldest_first():
    registry, _ = a_registry_with("triald", "console", "a browser tab")
    assert [o.name for o in registry.observers()] == ["triald", "console", "a browser tab"]


def test_a_name_is_self_declared_and_nothing_is_granted_by_it():
    # There is no registration to forge: the name is a label on a diagnostic
    # screen, so it is cleaned for display and otherwise taken as given.
    assert clean_name("triald") == "triald"
    assert clean_name(None) == UNNAMED
    assert clean_name("") == UNNAMED
    assert clean_name("   ") == UNNAMED


def test_a_name_cannot_carry_control_characters_onto_a_screen():
    assert clean_name("tri\nald\t") == "triald"
    assert clean_name("x" * 200) == "x" * 64
