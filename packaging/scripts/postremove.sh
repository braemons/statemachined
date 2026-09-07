#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# What is left behind, and what is not.
#
# The state directory stays. /var/lib/braemons/statemachined holds the graph
# store, the state-machine configs somebody authored in the web UI, and the
# recordings -- an experiment's data, which a package removal is not permission
# to delete. `dpkg --purge` does not delete it either; that is a decision for a
# person with `rm`, who at least knows what is in there.
set -e

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload >/dev/null 2>&1 || true
fi
if command -v udevadm >/dev/null 2>&1; then
  udevadm control --reload-rules >/dev/null 2>&1 || true
fi
