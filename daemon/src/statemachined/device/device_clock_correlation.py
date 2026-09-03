# SPDX-License-Identifier: LGPL-3.0-or-later
"""Device microseconds into host time, and the honesty about how well.

dev/PLAN.md calls this load-bearing and it is worth restating why. The device
timestamps in its own microseconds, which wrap every ~71 minutes and whose
origin is whenever that board was last powered. triald can only ask vstimd "was
there frame loss during trial 42" if trial 42's window arrives in a clock vstimd
shares. Hand over raw device microseconds and the question cannot be asked at
all.

Two separate jobs, and conflating them is how this kind of code goes wrong:

  * **Unwrapping.** A 32-bit microsecond counter wraps every 71.6 minutes, so
    the raw numbers in a session are not even monotonic. Unwrapping needs to see
    the timestamps in order, which is exactly what the `visit` stream provides
    and what its `seq` makes checkable.

  * **Correlating.** Turning an unwrapped device timestamp into a host one needs
    an offset, and the offset can only be estimated -- from a `ping` round trip,
    which bounds it but does not determine it. So every estimate carries its
    uncertainty, and the raw value is never discarded: **the raw value is the
    evidence and the correlation is an estimate**, and a record that lost the
    difference could not be re-derived later when somebody has a better one.

What this deliberately does not do is model drift. Two clocks that differ in
rate will diverge, and the right answer for a session that runs for hours is to
keep pinging -- which the supervisor does anyway for the link-loss watchdog, so
the estimate is refreshed for free. Fitting a line through the observations
would be better and is not v1: an unmeasured drift correction is worse than an
honest offset with a stated uncertainty.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

#: The device clock is a `uint32_t` of microseconds, so it wraps here.
DEVICE_CLOCK_WRAP_MICROSECONDS = 1 << 32

#: How far backwards a raw reading may jump before it is read as a wrap rather
#: than as two readings arriving out of order. Half the range is the only
#: threshold that cannot be fooled by ordinary jitter, and it is what makes
#: unwrapping correct as long as no gap exceeds ~35 minutes.
WRAP_DETECTION_THRESHOLD_MICROSECONDS = DEVICE_CLOCK_WRAP_MICROSECONDS // 2


@dataclass(frozen=True)
class HostTimeEstimate:
    """A device timestamp in host time, with how well that is known.

    `uncertainty_microseconds` is half the round trip of the `ping` the estimate
    rests on: the device read its clock somewhere inside that window, and
    nothing on this side can say where. It is a bound, not a standard
    deviation.
    """

    host_unix_seconds: float
    uncertainty_microseconds: int

    @property
    def host_time_iso8601(self) -> str:
        """UTC, to the microsecond, the way a trace line carries it."""
        seconds = int(self.host_unix_seconds)
        microseconds = round((self.host_unix_seconds - seconds) * 1_000_000)
        formatted = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
        return f"{formatted}.{microseconds:06d}Z"


class DeviceClockCorrelation:
    """One device's clock, unwrapped and tied to this host's.

    Lives as long as a connection. A device that resets restarts its clock from
    zero, so a reconnect must construct a new one rather than carry an offset
    across -- see `forget_everything_observed`.
    """

    def __init__(self) -> None:
        self._last_raw_device_microseconds: int | None = None
        self._accumulated_wraps = 0
        self._best_round_trip_microseconds: int | None = None
        self._host_unix_seconds_at_best_observation = 0.0
        self._unwrapped_device_microseconds_at_best_observation = 0

    # ------------------------------------------------------------ unwrap ---

    def unwrap_device_microseconds(self, raw_device_microseconds: int) -> int:
        """A raw reading into a monotonic timeline for this connection.

        Must see the readings **in order**, which is why the caller is the thing
        that reads the `visit` stream: it is the only place in the daemon that
        sees every device timestamp exactly once and in sequence. A `seq` gap
        there is what says this cannot be trusted.
        """
        if self._last_raw_device_microseconds is not None:
            went_backwards = raw_device_microseconds < self._last_raw_device_microseconds
            jumped_far = (
                self._last_raw_device_microseconds - raw_device_microseconds
                > WRAP_DETECTION_THRESHOLD_MICROSECONDS
            )
            if went_backwards and jumped_far:
                self._accumulated_wraps += 1
        self._last_raw_device_microseconds = raw_device_microseconds
        return self._accumulated_wraps * DEVICE_CLOCK_WRAP_MICROSECONDS + raw_device_microseconds

    # --------------------------------------------------------- correlate ---

    def observe_ping_round_trip(
        self,
        host_unix_seconds_before_request: float,
        raw_device_microseconds_in_reply: int,
        host_unix_seconds_after_reply: float,
    ) -> None:
        """Fold in one `ping`/`pong`, keeping it only if it is the tightest yet.

        The best observation is the one with the shortest round trip, because
        the round trip *is* the uncertainty: the device read its clock somewhere
        between the two host readings and nothing can narrow that further. A
        later, sloppier ping on a busy link should not replace a tighter earlier
        one, so this keeps the best rather than the newest.
        """
        # Rounded, not truncated. Truncating biases the bound *downward*, and a
        # bound that understates itself is worse than no bound: it would claim
        # the offset is known better than it is.
        round_trip_microseconds = round(
            (host_unix_seconds_after_reply - host_unix_seconds_before_request) * 1_000_000
        )
        if round_trip_microseconds < 0:
            raise ValueError("the reply arrived before the request was sent")
        if (
            self._best_round_trip_microseconds is not None
            and round_trip_microseconds >= self._best_round_trip_microseconds
        ):
            return

        self._best_round_trip_microseconds = round_trip_microseconds
        # The device read its clock at an unknown point inside the window, so
        # the midpoint is the estimate and half the window is the bound.
        self._host_unix_seconds_at_best_observation = (
            host_unix_seconds_before_request + host_unix_seconds_after_reply
        ) / 2
        self._unwrapped_device_microseconds_at_best_observation = self.unwrap_device_microseconds(
            raw_device_microseconds_in_reply
        )

    @property
    def has_an_estimate(self) -> bool:
        return self._best_round_trip_microseconds is not None

    @property
    def best_round_trip_microseconds(self) -> int | None:
        return self._best_round_trip_microseconds

    def host_time_for_unwrapped_device_microseconds(
        self, unwrapped_device_microseconds: int
    ) -> HostTimeEstimate | None:
        """Where a device timestamp falls on this host's clock, or None.

        Takes an **unwrapped** value, and that is not fussiness: unwrapping is
        stateful and order-dependent, so a query that unwrapped its own argument
        would advance the sequence and corrupt the next real reading. The caller
        unwraps once, in order, as the timestamps arrive.

        None when no `ping` has been answered yet, and that is deliberate: a
        made-up offset would be indistinguishable in the record from a measured
        one, and the raw value is still there for whoever wants to correlate it
        later with a better estimate.
        """
        if self._best_round_trip_microseconds is None:
            return None
        elapsed_microseconds = (
            unwrapped_device_microseconds
            - self._unwrapped_device_microseconds_at_best_observation
        )
        return HostTimeEstimate(
            host_unix_seconds=self._host_unix_seconds_at_best_observation
            + elapsed_microseconds / 1_000_000,
            uncertainty_microseconds=self._best_round_trip_microseconds // 2,
        )

    def forget_everything_observed(self) -> None:
        """A new connection: a reset device restarts its clock from zero.

        Carrying an offset across a reconnect would put every timestamp after it
        wrong by however long the board was away, and wrong *plausibly*, which
        is the worst kind.
        """
        self.__init__()
