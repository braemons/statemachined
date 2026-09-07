// SPDX-License-Identifier: GPL-3.0-or-later
// Who starts the trials.
//
// Everywhere else in this firmware the answer is "the host": triald is the
// decision authority, it chooses the trial type and arms each trial, and the
// device is the timing authority that runs the one it was given. Autorun is the
// case where there is no decision authority at all -- a board on a bench, a rig
// running an unsupervised shaping session, a box with nothing plugged into its
// USB port -- and the device starts the next run itself.
//
// It is deliberately a *device* setting rather than a property of a graph. A
// graph says how long to dwell in each terminal state before another run may
// begin (State::relight_duration), because an inter-trial interval is a
// paradigm decision that has to replay with the trial it followed. Whether
// anything acts on that dwell is a fact about the deployment, and the same
// graph must run unchanged under a host that arms every trial itself. So the
// timing is in the graph, the authority is here, and neither one can be
// mistaken for the other.
//
// The safety rule that comes with it: a host that greets takes the rig. See
// HostLinkSession::on_hello. Autorun then resumes only when it is asked for
// again, or at the next boot from stored settings -- a daemon that crashes must
// not leave a board delivering rewards to an animal nobody is watching.
#pragma once
#include <cstdint>

#include "config.h"

namespace statemachined {

/// What a self-driving board needs to know, and no more. Small and flat on
/// purpose: this is one of the things that goes to storage, and a settings
/// record whose shape is obvious is a settings record that can be read back by
/// a firmware that is not the one that wrote it.
struct AutorunConfig {
  /// Whether the device may start runs on its own. Stored, so a board can come
  /// up self-driving with nothing attached; see the takeover rule above for
  /// when it is honoured.
  bool enabled = false;

  /// Which graph of the committed set it runs. Autorun cannot switch paradigms
  /// -- switching is a decision, and the whole premise here is that nothing is
  /// making decisions.
  uint8_t graph_index = 0;

  /// Wall-clock cap on each run, as `configure`'s `cap_ms`. Zero means the
  /// device default. It matters more here than under a host: nobody is watching
  /// for a graph that has hung.
  Milliseconds cap_ms = 0;

  /// The stream every autorun trial's randomness is derived from. Carried here
  /// rather than taken from the session, because a self-driving board may never
  /// receive a `hello` and so may never be given one -- and a foreperiod drawn
  /// from a seed nothing recorded is a timing nobody can account for
  /// afterwards. Fixed and stored, so an unattended session replays.
  uint64_t seed = 0;

  /// Where the trial ids it assigns start. A board that has been running on its
  /// own and is then greeted has numbered its trials from here, and saying so
  /// is what lets a host tell those runs apart from its own.
  uint32_t first_trial_id = 1;
};

}  // namespace statemachined
