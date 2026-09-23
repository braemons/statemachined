# SPDX-License-Identifier: GPL-3.0-or-later
"""Every copy of the trace's timestamp formatting carries into the next second.

`round()` on a fraction within half a microsecond of a whole second gives
1_000_000, which formatted as six digits is seven -- `...:20.1000000Z` -- on a
second that is also wrong by one. `device_clock_correlation` was fixed for this
and has its own test; these are the three other copies of the same function,
which write every `recorded_host_time` in the trace, the recording and the
serial monitor.
"""

from __future__ import annotations

import pytest

from statemachined.daemon import event_recording
from statemachined.device import device_line_monitor, state_visit_trace

FORMATTERS = [
    pytest.param(state_visit_trace._iso8601_utc, id="trace"),
    pytest.param(event_recording._iso8601_utc, id="recording"),
    pytest.param(device_line_monitor._iso8601_utc, id="line monitor"),
]


@pytest.mark.parametrize("iso8601_utc", FORMATTERS)
def test_half_a_microsecond_under_a_second_is_that_second(iso8601_utc):
    assert iso8601_utc(1_700_000_000.9999996) == "2023-11-14T22:13:21.000000Z"


@pytest.mark.parametrize("iso8601_utc", FORMATTERS)
def test_the_fraction_is_always_six_digits(iso8601_utc):
    for unix_seconds in (1_700_000_000.0, 1_700_000_000.5, 1_700_000_000.9999994):
        assert len(iso8601_utc(unix_seconds)) == len("2023-11-14T22:13:20.000000Z")
