# SPDX-License-Identifier: GPL-3.0-or-later
"""The record that says this rig exists.

Nothing here registers anything on the network: what is worth testing is the
record's *shape* and the promise that a failure to publish it never takes the
daemon with it -- a rig with no Avahi still owns a device, and a daemon that
refused to start over an mDNS socket would be unadministrable exactly when
somebody needs the API to find out why.
"""

from __future__ import annotations

from pathlib import Path

from statemachined.mdns_service_advertisement import (
    SERVICE_TYPE,
    MdnsServiceAdvertisement,
    stable_rig_identifier,
    text_records_for,
)


def test_the_identifier_is_stable_and_is_not_the_machine_id(tmp_path: Path) -> None:
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("dc4f4b06f2d84f6b9e2a7b0c1d2e3f40\n")

    identifier = stable_rig_identifier(machine_id)
    assert identifier == stable_rig_identifier(machine_id)
    assert len(identifier) == 16
    # A machine-id is meant to be treated as confidential, and what a console
    # needs is a stable identifier rather than that particular one.
    assert "dc4f4b06" not in identifier


def test_two_boxes_get_different_identifiers(tmp_path: Path) -> None:
    (tmp_path / "one").write_text("11111111111111111111111111111111")
    (tmp_path / "two").write_text("22222222222222222222222222222222")
    assert stable_rig_identifier(tmp_path / "one") != stable_rig_identifier(tmp_path / "two")


def test_a_box_with_no_machine_id_still_gets_one(tmp_path: Path) -> None:
    """A container, a Mac, a developer's checkout. Weaker -- a rename changes it
    -- and still better than a value that changes every restart, which would
    make a console show one rig as many."""
    assert len(stable_rig_identifier(tmp_path / "absent")) == 16


def test_the_text_records_say_where_the_api_and_the_elements_are() -> None:
    records = text_records_for(
        rig_identifier="abc123", port=8081, device_target="/dev/ttyACM0", version="1.2.3"
    )
    assert records[b"id"] == b"abc123"
    assert records[b"api"] == b"/api"
    # A console has to construct the elements URL, and constructing it from a
    # convention rather than from the record is how a console breaks when the
    # daemon is behind a proxy.
    assert records[b"elements"] == b"/elements/statemachined.js"
    assert records[b"device"] == b"/dev/ttyACM0"
    assert records[b"version"] == b"1.2.3"


def test_the_service_name_is_under_the_braemons_service_type() -> None:
    advertisement = MdnsServiceAdvertisement(port=8081, instance_name="rig-3")
    assert advertisement.service_name() == f"rig-3.{SERVICE_TYPE}"


def test_the_record_carries_the_port_it_was_told_to_advertise() -> None:
    info = MdnsServiceAdvertisement(port=9099, instance_name="rig-3").build_service_info()
    assert info.port == 9099
    assert info.properties[b"port"] == b"9099"


def test_a_failure_to_advertise_is_reported_and_not_raised(monkeypatch) -> None:
    """The whole reason this is a small class rather than three lines in
    `create_application`."""
    said = []
    advertisement = MdnsServiceAdvertisement(port=8081, report=said.append)
    monkeypatch.setattr(
        advertisement,
        "build_service_info",
        lambda: (_ for _ in ()).throw(OSError("no such device")),
    )

    assert advertisement.start() is False
    assert advertisement.is_advertising is False
    assert "no such device" in advertisement.last_failure
    assert said and "The API is still served" in said[0]
    # And stopping something that never started is not a second failure.
    advertisement.stop()
