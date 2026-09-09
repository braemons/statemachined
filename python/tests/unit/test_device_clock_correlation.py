# SPDX-License-Identifier: GPL-3.0-or-later
"""The wrap, the offset, and the honesty about the offset.

None of this can be tested against a device in any reasonable time: the device
clock wraps every 71.6 minutes, and the whole point of the correlation is what
it does when it has no observation yet. So it is arithmetic, tested as
arithmetic.
"""

from __future__ import annotations

import pytest

from statemachined.device.device_clock_correlation import (
    DEVICE_CLOCK_WRAP_MICROSECONDS,
    DeviceClockCorrelation,
)


def test_readings_that_climb_are_left_alone():
    clock = DeviceClockCorrelation()
    assert clock.unwrap_device_microseconds(1000) == 1000
    assert clock.unwrap_device_microseconds(2000) == 2000


def test_a_reading_that_falls_off_the_end_of_the_counter_carries():
    # 71.6 minutes into a session, and a trial that straddles it must not report
    # a negative duration or a state that lasted an hour.
    clock = DeviceClockCorrelation()
    just_before_the_wrap = DEVICE_CLOCK_WRAP_MICROSECONDS - 1000
    assert clock.unwrap_device_microseconds(just_before_the_wrap) == just_before_the_wrap
    assert clock.unwrap_device_microseconds(500) == DEVICE_CLOCK_WRAP_MICROSECONDS + 500


def test_a_small_step_backwards_is_not_read_as_a_wrap():
    # Two messages arriving out of order, or a timestamp read a moment earlier
    # than the one before it. Treating that as a wrap would add 71 minutes to
    # everything that followed.
    clock = DeviceClockCorrelation()
    clock.unwrap_device_microseconds(5_000_000)
    assert clock.unwrap_device_microseconds(4_999_000) == 4_999_000


def test_several_wraps_accumulate():
    clock = DeviceClockCorrelation()
    for lap in range(3):
        clock.unwrap_device_microseconds(DEVICE_CLOCK_WRAP_MICROSECONDS - 1)
        expected = (lap + 1) * DEVICE_CLOCK_WRAP_MICROSECONDS
        assert clock.unwrap_device_microseconds(0) == expected


# ------------------------------------------------------------- correlation ---


def test_there_is_no_estimate_until_a_ping_has_been_answered():
    # A made-up offset would be indistinguishable in the record from a measured
    # one. The raw value is still there for whoever wants to correlate it later.
    clock = DeviceClockCorrelation()
    assert not clock.has_an_estimate
    assert clock.host_time_for_unwrapped_device_microseconds(1_000_000) is None


def test_an_estimate_puts_a_device_timestamp_on_the_host_clock():
    clock = DeviceClockCorrelation()
    # A ping sent at t=1000.000 and answered by t=1000.002, with the device
    # saying its clock read 500000 us.
    clock.observe_ping_round_trip(1000.000, 500_000, 1000.002)
    estimate = clock.host_time_for_unwrapped_device_microseconds(1_500_000)
    assert estimate is not None
    # One device second later than the observation, which was taken at the
    # midpoint of the round trip.
    assert estimate.host_unix_seconds == pytest.approx(1001.001, abs=1e-6)
    # Half the round trip: the device read its clock somewhere in that window
    # and nothing on this side can say where.
    assert estimate.uncertainty_microseconds == 1000


def test_the_tightest_round_trip_wins_rather_than_the_most_recent():
    # A later, sloppier ping on a busy link should not replace a tighter earlier
    # one: the round trip *is* the uncertainty.
    clock = DeviceClockCorrelation()
    clock.observe_ping_round_trip(1000.000, 500_000, 1000.002)
    clock.observe_ping_round_trip(1010.000, 10_500_000, 1010.500)
    assert clock.best_round_trip_microseconds == 2000


def test_a_tighter_round_trip_replaces_a_looser_one():
    clock = DeviceClockCorrelation()
    clock.observe_ping_round_trip(1000.000, 500_000, 1000.500)
    clock.observe_ping_round_trip(1010.000, 10_500_000, 1010.002)
    assert clock.best_round_trip_microseconds == 2000
    estimate = clock.host_time_for_unwrapped_device_microseconds(10_500_000)
    assert estimate is not None
    assert estimate.host_unix_seconds == pytest.approx(1010.001, abs=1e-6)


def test_a_reply_that_arrives_before_it_was_sent_is_refused():
    clock = DeviceClockCorrelation()
    with pytest.raises(ValueError, match="before the request was sent"):
        clock.observe_ping_round_trip(1000.0, 1, 999.0)


def test_a_new_connection_forgets_everything():
    # A reset device restarts its clock from zero, so carrying an offset across
    # would put every later timestamp wrong by however long the board was away.
    clock = DeviceClockCorrelation()
    clock.observe_ping_round_trip(1000.0, 500_000, 1000.002)
    clock.unwrap_device_microseconds(DEVICE_CLOCK_WRAP_MICROSECONDS - 1)
    clock.unwrap_device_microseconds(0)

    clock.forget_everything_observed()
    assert not clock.has_an_estimate
    assert clock.unwrap_device_microseconds(1000) == 1000


def test_the_host_time_reads_as_the_trace_writes_it():
    clock = DeviceClockCorrelation()
    clock.observe_ping_round_trip(0.0, 0, 0.0)
    estimate = clock.host_time_for_unwrapped_device_microseconds(1_500_000)
    assert estimate is not None
    assert estimate.host_time_iso8601 == "1970-01-01T00:00:01.500000Z"
