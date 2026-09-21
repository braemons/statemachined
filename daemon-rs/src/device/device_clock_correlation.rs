// SPDX-License-Identifier: AGPL-3.0-or-later
//! Device microseconds into host time, and the honesty about how well.
//!
//! **Load-bearing.** The device timestamps in its own microseconds, which wrap
//! every ~71 minutes and whose origin is whenever that board was last powered.
//! triald can only ask vstimd "was there frame loss during trial 42" if trial
//! 42's window arrives in a clock vstimd shares. Hand over raw device
//! microseconds and the question cannot be asked at all.
//!
//! Two separate jobs, and **conflating them is how this kind of code goes
//! wrong**:
//!
//! * **Unwrapping.** A 32-bit microsecond counter wraps every 71.6 minutes, so
//!   the raw numbers in a session are not even monotonic. Unwrapping needs to
//!   see the timestamps in order, which is exactly what the `visit` stream
//!   provides and what its `seq` makes checkable.
//! * **Correlating.** Turning an unwrapped timestamp into a host one needs an
//!   offset, and the offset can only be estimated — from a `ping` round trip,
//!   which bounds it but does not determine it. So every estimate carries its
//!   uncertainty and **the raw value is never discarded: the raw value is the
//!   evidence and the correlation is an estimate.**
//!
//! What this deliberately does not do is model drift. Two clocks that differ in
//! rate will diverge, and the right answer for a session that runs for hours is
//! to keep pinging — which the supervisor does anyway for the link-loss
//! watchdog, so the estimate is refreshed for free. An unmeasured drift
//! correction is worse than an honest offset with a stated uncertainty.

/// The device clock is a `uint32_t` of microseconds, so it wraps here.
pub const DEVICE_CLOCK_WRAP_MICROSECONDS: i128 = 1 << 32;

/// How far backwards a raw reading may jump before it is read as a wrap rather
/// than as two readings arriving out of order.
///
/// Half the range is the only threshold that cannot be fooled by ordinary
/// jitter, and it is what makes unwrapping correct as long as no gap exceeds
/// ~35 minutes.
pub const WRAP_DETECTION_THRESHOLD_MICROSECONDS: i128 = DEVICE_CLOCK_WRAP_MICROSECONDS / 2;

/// A device timestamp in host time, with how well that is known.
///
/// `uncertainty_microseconds` is half the round trip of the `ping` the estimate
/// rests on: the device read its clock somewhere inside that window, and nothing
/// on this side can say where. **It is a bound, not a standard deviation.**
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct HostTimeEstimate {
    pub host_unix_seconds: f64,
    pub uncertainty_microseconds: i64,
}

impl HostTimeEstimate {
    /// UTC, to the microsecond, the way a trace line carries it.
    pub fn host_time_iso8601(&self) -> String {
        let mut seconds = self.host_unix_seconds.trunc() as i64;
        let mut microseconds =
            ((self.host_unix_seconds - seconds as f64) * 1_000_000.0).round() as i64;
        // A fraction just under a whole second rounds up to 1_000_000, which
        // would print as `.1000000Z` -- seven digits, and not a time. Carry it
        // instead. The Python implementation has this hole; see
        // `tests/clock.rs`.
        if microseconds >= 1_000_000 {
            seconds += 1;
            microseconds -= 1_000_000;
        }
        if microseconds < 0 {
            seconds -= 1;
            microseconds += 1_000_000;
        }
        format!("{}.{microseconds:06}Z", utc_of(seconds))
    }
}

/// `%Y-%m-%dT%H:%M:%S` for a Unix second, in UTC.
///
/// Written out rather than taken from a date crate: this is one format, it is
/// the one a trace line carries, and the civil-time arithmetic below is the
/// whole of what a dependency would provide.
fn utc_of(unix_seconds: i64) -> String {
    let days = unix_seconds.div_euclid(86_400);
    let second_of_day = unix_seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}",
        second_of_day / 3600,
        (second_of_day % 3600) / 60,
        second_of_day % 60
    )
}

/// Howard Hinnant's `civil_from_days`, which is the standard way to do this
/// without a table and is exact for every day this daemon will see.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let day_of_era = z.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = (day_of_year - (153 * month_prime + 2) / 5 + 1) as u32;
    let month = if month_prime < 10 {
        month_prime + 3
    } else {
        month_prime - 9
    } as u32;
    (if month <= 2 { year + 1 } else { year }, month, day)
}

/// One device's clock, unwrapped and tied to this host's.
///
/// Lives as long as a connection. **A device that resets restarts its clock
/// from zero**, so a reconnect must construct a new one rather than carry an
/// offset across.
#[derive(Debug, Default)]
pub struct DeviceClockCorrelation {
    last_raw_device_microseconds: Option<i128>,
    accumulated_wraps: i128,
    best_round_trip_microseconds: Option<i64>,
    host_unix_seconds_at_best_observation: f64,
    unwrapped_device_microseconds_at_best_observation: i128,
}

impl DeviceClockCorrelation {
    pub fn new() -> Self {
        Self::default()
    }

    /// A raw reading into a monotonic timeline for this connection.
    ///
    /// Must see the readings **in order**, which is why the caller is the thing
    /// that reads the `visit` stream: it is the only place in the daemon that
    /// sees every device timestamp exactly once and in sequence. A `seq` gap
    /// there is what says this cannot be trusted.
    pub fn unwrap_device_microseconds(&mut self, raw_device_microseconds: i128) -> i128 {
        if let Some(last) = self.last_raw_device_microseconds {
            let went_backwards = raw_device_microseconds < last;
            let jumped_far =
                last - raw_device_microseconds > WRAP_DETECTION_THRESHOLD_MICROSECONDS;
            if went_backwards && jumped_far {
                self.accumulated_wraps += 1;
            }
        }
        self.last_raw_device_microseconds = Some(raw_device_microseconds);
        self.accumulated_wraps * DEVICE_CLOCK_WRAP_MICROSECONDS + raw_device_microseconds
    }

    /// Fold in one `ping`/`pong`, keeping it only if it is the tightest yet.
    ///
    /// **The best observation is the one with the shortest round trip**,
    /// because the round trip *is* the uncertainty: the device read its clock
    /// somewhere between the two host readings and nothing can narrow that
    /// further. A later, sloppier ping on a busy link should not replace a
    /// tighter earlier one, so this keeps the best rather than the newest.
    pub fn observe_ping_round_trip(
        &mut self,
        host_unix_seconds_before_request: f64,
        raw_device_microseconds_in_reply: i128,
        host_unix_seconds_after_reply: f64,
    ) -> Result<(), String> {
        // Rounded, not truncated. Truncating biases the bound *downward*, and a
        // bound that understates itself is worse than no bound: it would claim
        // the offset is known better than it is.
        let round_trip_microseconds = ((host_unix_seconds_after_reply
            - host_unix_seconds_before_request)
            * 1_000_000.0)
            .round() as i64;
        if round_trip_microseconds < 0 {
            return Err("the reply arrived before the request was sent".into());
        }
        if self
            .best_round_trip_microseconds
            .is_some_and(|best| round_trip_microseconds >= best)
        {
            return Ok(());
        }
        self.best_round_trip_microseconds = Some(round_trip_microseconds);
        // The device read its clock at an unknown point inside the window, so
        // the midpoint is the estimate and half the window is the bound.
        self.host_unix_seconds_at_best_observation =
            (host_unix_seconds_before_request + host_unix_seconds_after_reply) / 2.0;
        self.unwrapped_device_microseconds_at_best_observation =
            self.unwrap_device_microseconds(raw_device_microseconds_in_reply);
        Ok(())
    }

    pub fn has_an_estimate(&self) -> bool {
        self.best_round_trip_microseconds.is_some()
    }

    pub fn best_round_trip_microseconds(&self) -> Option<i64> {
        self.best_round_trip_microseconds
    }

    /// Where a device timestamp falls on this host's clock, or `None`.
    ///
    /// Takes an **unwrapped** value, and that is not fussiness: unwrapping is
    /// stateful and order-dependent, so a query that unwrapped its own argument
    /// would advance the sequence and corrupt the next real reading.
    ///
    /// `None` when no `ping` has been answered yet, and that is deliberate: a
    /// made-up offset would be indistinguishable in the record from a measured
    /// one, and the raw value is still there for whoever wants to correlate it
    /// later with a better estimate.
    pub fn host_time_for_unwrapped_device_microseconds(
        &self,
        unwrapped_device_microseconds: i128,
    ) -> Option<HostTimeEstimate> {
        let best = self.best_round_trip_microseconds?;
        let elapsed_microseconds =
            unwrapped_device_microseconds - self.unwrapped_device_microseconds_at_best_observation;
        Some(HostTimeEstimate {
            host_unix_seconds: self.host_unix_seconds_at_best_observation
                + elapsed_microseconds as f64 / 1_000_000.0,
            // Integer floor, as the Python `//` is.
            uncertainty_microseconds: best.div_euclid(2),
        })
    }

    /// A new connection: a reset device restarts its clock from zero.
    ///
    /// Carrying an offset across a reconnect would put every timestamp after it
    /// wrong by however long the board was away, and wrong **plausibly**, which
    /// is the worst kind.
    pub fn forget_everything_observed(&mut self) {
        *self = Self::new();
    }
}
