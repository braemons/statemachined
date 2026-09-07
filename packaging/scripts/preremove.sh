#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Stop and disable, but only when the package is actually going away.
#
# An upgrade also runs this, and stopping there would drop the port mid-session
# for a restart the postinstall is about to do anyway.
set -e

removing=yes
case "${1:-}" in
  upgrade|1) removing=no ;;      # dpkg says `upgrade`, rpm says 1
esac

if [ "$removing" = yes ] && command -v systemctl >/dev/null 2>&1; then
  systemctl stop statemachined.service >/dev/null 2>&1 || true
  systemctl disable statemachined.service >/dev/null 2>&1 || true
fi
