#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# After the files land: the user, the device rule, the unit.
#
# Deliberately does **not** start the daemon on a first install. Starting it
# would open the serial port and greet whatever is on it, and greeting takes the
# rig -- so an install onto a box with a board already running unattended would
# stop it, before anybody had looked at the config naming which board it is. An
# upgrade is different: something that was running should still be running
# afterwards, so that case restarts.
set -e

# systemd-sysusers rather than adduser/useradd, which differ per distro.
if [ -x /usr/bin/systemd-sysusers ] || [ -x /bin/systemd-sysusers ]; then
  systemd-sysusers /usr/lib/sysusers.d/statemachined.conf >/dev/null 2>&1 || true
fi

# The rule grants the board to that user, so it has to be applied before the
# daemon is any use -- and a board already plugged in when the package landed
# will not be re-enumerated on its own.
if command -v udevadm >/dev/null 2>&1; then
  udevadm control --reload-rules >/dev/null 2>&1 || true
  udevadm trigger --subsystem-match=tty >/dev/null 2>&1 || true
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl enable statemachined.service >/dev/null 2>&1 || true
fi

# dpkg passes "configure" with the old version as $2 on an upgrade and nothing
# on a first install; rpm passes 1 and 2. Both shapes, one test.
upgrade=no
case "${1:-}" in
  configure) [ -n "${2:-}" ] && upgrade=yes ;;
  2|*[0-9]*) [ "${1:-}" != "1" ] && upgrade=yes ;;
esac

if [ "$upgrade" = yes ]; then
  systemctl try-restart statemachined.service >/dev/null 2>&1 || true
else
  cat <<'NOTE'

statemachined is installed and enabled, and is not running yet.

  1. Say what this rig is:   /etc/braemons/statemachined-rig-config.toml
     -- above all `device_target` and `expected_board`.
  2. Plug the board in and check the rule took:  ls -l /dev/braemons/
  3. Start it:               sudo systemctl start statemachined
  4. Open it:                http://<this host>:8081/

It is not started for you because starting it greets the board, and greeting a
board takes the rig: one that was running trials on its own would stop.

NOTE
fi
