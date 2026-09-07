# Packaging

The daemon as a rig installs it: `.deb` and `.rpm`, a systemd unit, a udev rule
that says *which* board and *who* may open it, and a conffile describing what
the box is.

```sh
make -C packaging install-nfpm     # once
make image                         # optional: the flashable firmware goes in too
make -C packaging deb              # build/package/dist/statemachined_<version>_<arch>.deb
make -C packaging inspect          # what went in, without installing it
```

`make deb` from the repository root is the same thing.

## What the package is

**A vendored interpreter, not a set of `python3-*` dependencies.** The whole
tree — CPython from python-build-standalone, the daemon, and its five runtime
dependencies — lands under `/opt/braemons/statemachined` and behaves like a
compiled binary. It costs about 45 MB and makes the packages
per-architecture despite being pure Python, and it buys the thing a rig
actually needs: an operating system upgrade that moves Python cannot stop an
experiment, and a box in a rack does not need a working `pip` to be repaired.

**One `nfpm.yaml`, both formats.** A `debian/` directory plus a hand-written
`.spec` would be the same list of files written twice, and the second copy is
the one that goes wrong.

**The version comes from the git tag.** `daemon/pyproject.toml` carries a
`0.0.0` sentinel and `scripts/git-version.sh` stamps the real version into a
*copy* at build time, so a build never dirties the working tree — and a `0.0.0`
artifact in the wild means the stamping was bypassed rather than that somebody
forgot to bump a number.

## The files

| | |
|---|---|
| `Makefile` | stages the tree, checks it, and hands it to nfpm |
| `nfpm.yaml` | what goes where, and what is a conffile |
| `scripts/git-version.sh` | the version, in its package form and its PEP 440 form |
| `scripts/check-staged-tree.py` | runs the interpreter that is about to be shipped |
| `scripts/{postinstall,preremove,postremove}.sh` | the user, the rule, the unit |
| `systemd/statemachined.service` | how it runs, and what it is not allowed to touch |
| `sysusers/statemachined.conf` | the unprivileged account |
| `udev/60-statemachined.rules` | which port, and who may open it |
| `logrotate/statemachined` | the trace tail, which is nobody's to keep |
| `etc/statemachined-rig-config.toml` | what the box is. Hand-edited, never written by the daemon |
| `etc/statemachined.env` | where the box is reached: `--host`, `--port` |

## Three decisions worth knowing before you change something

**The daemon is enabled but not started on a first install.** Starting it opens
the serial port and greets whatever is on it, and greeting *takes the rig*
(`dev/PROTOCOL.md` §3.7) — so an install onto a box whose board was running
unattended would stop it, before anybody had looked at the config naming which
board it even is. An *upgrade* restarts, because something that was running
should still be running afterwards.

**The board is granted by a udev rule, not by a group.** Adding the daemon's
user to `dialout` (Debian) or `uucp` (Fedora) grants it every serial device on
the box, and it is not the same group on both. The rule matches the R4's
VID/PID, grants that node to the `statemachined` user, and makes a stable
`/dev/braemons/statemachined0` — which also answers *which* port on a Pi with
several USB devices, and `/dev/ttyACM0` does not.

**Removal leaves `/var/lib/braemons/statemachined` alone**, `--purge`
included. It holds the graph store, the state-machine configs somebody authored
in the web UI, and the recordings: an experiment's data, which a package
removal is not permission to delete.

## Cross-building

Not attempted. The interpreter is a native artifact, so an arm64 package is
built on an arm64 machine — a Pi, or a container on one — and `make deb`
refuses to guess rather than labelling a package with an architecture it was
not built on.

## After installing

```sh
sudoedit /etc/braemons/statemachined-rig-config.toml   # device_target, expected_board
ls -l /dev/braemons/                                   # did the rule take?
sudo systemctl start statemachined
xdg-open http://localhost:8081/
```

`journalctl -u statemachined -f` is the log. The rest is `dev/DAEMON.md`, which
ships in `/usr/share/doc/statemachined/`.
