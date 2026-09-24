# Packaging

The daemon as a rig installs it: `.deb` and `.rpm`, a systemd unit, a udev rule
that says *which* board and *who* may open it, and a conffile describing what
the box is.

```sh
make image                         # the flashable firmware goes into the package
make -C packaging packages         # both architectures, both formats, into dist/
make -C packaging packages-arm64   # just the Pi 5
make -C packaging inspect          # what went in, without installing it
```

That builds inside a pinned container. For iterating, the same recipe runs
natively on this machine — quicker, and correct for this architecture only:

```sh
make -C packaging install-nfpm     # once
make -C packaging deb              # or `make deb` from the repository root
make -C packaging repro            # build twice, prove the bytes match
```

arm64 runs the builder image under qemu, so register the handler once per boot:

```sh
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
```

## What the package is

**`braemons-statemachined`,** like every package in the braemons archive — the
prefix is the archive's, so `apt search braemons` answers "what is on this rig"
and a name as generic as `statemachined` cannot collide with something a
distribution ships. Inside the package nothing carries it: the binary, the unit,
the user and the logrotate entry are all plain `statemachined`, because that is
what an operator types. `sudo apt install ./braemons-statemachined_*_arm64.deb`.

**One binary.** The daemon is Rust, and its web UI is embedded in it, so the
package is `/usr/bin/statemachined`, the unit files, the conffiles, the docs and
the flashable firmware — mousewheeld's shape. Nothing to vendor and no runtime
dependency but glibc, so an operating system upgrade cannot stop an experiment
by moving an interpreter. Debug symbols stay in, as mousewheeld's do: a stall on
a rig is diagnosed where it happens.

**One `nfpm.yaml`, both formats.** A `debian/` directory plus a hand-written
`.spec` would be the same list of files written twice, and the second copy is
the one that goes wrong. vstimd needs a builder image per format because
cargo-deb and rpmbuild are different tools; nfpm packs both from one staged
tree, so the matrix here is by architecture alone.

**Built in a pinned container, and reproducible.** Base image pinned by digest,
rustc and nfpm by version, dependencies from `Cargo.lock` (`--locked`) rather
than whatever a registry published since, paths in the binary remapped so they
do not name the machine that built it, and every mtime taken from the commit. Two builds of one commit are
then the same bytes: `make -C packaging repro` builds twice and compares, and CI
runs it.

Worth *checking* rather than asserting, because it fails silently — the package
still installs perfectly. All four of the above were found that way rather than
predicted. The one that would never have been guessed: uv leaves a
`uv_cache.json` in the dist-info recording the nanosecond of the install.

Reproducible *for a given builder*. The container and a native `make deb` on
this machine produce different bytes — different base OS, so different wheel
selection — which is exactly why what a release publishes comes out of the
container.

**Not a cross-compile.** The arm64 package is built by running the builder
image *as* arm64 under qemu, so one recipe serves both architectures and the
check at its end runs the binary it is about to pack. Slower than vstimd's
cross toolchain, and much simpler.

**The version comes from the git tag.** `scripts/git-version.sh` derives it, and
the build stamps it into the binary at compile time (`STATEMACHINED_VERSION`), so
a build never dirties the working tree. A binary built from a checkout without
it reports the crate's own version, which says it was not packaged.

## The files

| | |
|---|---|
| `Makefile` | builds the binary, stages the tree, runs it, and hands it to nfpm |
| `docker/Dockerfile.package-builder` | the pinned image both architectures are built in |
| `nfpm.yaml` | what goes where, and what is a conffile |
| `scripts/git-version.sh` | the version, in its package form |
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
(`docs/reference/protocol.md` §3.7) — so an install onto a box whose board was running
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

## Releases

`.github/workflows/release.yml`, from vstimd's. A `v*` tag builds the firmware
first — it goes *into* the packages, at
`/usr/share/braemons/statemachined/firmware/`, which
`GET /api/device/firmware` compares a connected board against — then both
architectures in parallel, then publishes the `.deb`s, the `.rpm`s, the `.bin`
and the `MANIFEST.txt` on a GitHub Release. A hyphen in the tag marks a
pre-release.

The version comes from the tag and nothing else: `git-version.sh` **refuses**
to invent one, because a wrong-but-plausible version baked into a package is
worse than a failed build. Pass `STATEMACHINED_VERSION=` to build outside a
tagged checkout — the container does exactly that, since its build context has
no `.git`.

Ingestion into the braemons archive is one line in `braemons/packages/sources.txt`:

```
braemons/statemachined
```

That is the whole integration. This repository needs no workflow changes for it
and holds no credentials, and a `~` in the version routes a pre-release to
`testing` rather than `stable`.

## After installing

```sh
sudoedit /etc/braemons/statemachined-rig-config.toml   # device_target, expected_board
ls -l /dev/braemons/                                   # did the rule take?
sudo systemctl start statemachined
xdg-open http://localhost:8081/
```

`journalctl -u statemachined -f` is the log. The rest is `docs/developer/daemon.md`, which
ships in `/usr/share/doc/braemons-statemachined/`.
