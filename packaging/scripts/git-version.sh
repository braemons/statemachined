#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# What version this working tree is, from the tag rather than from a number
# somebody maintains by hand.
#
# `daemon/pyproject.toml` carries the `0.0.0` sentinel and this stamps the real
# one at build time, so a `0.0.0` artifact in the wild means the stamping was
# bypassed rather than that the number was forgotten. That is the whole point of
# the sentinel: a package whose version is a lie is a package nobody can
# correlate with a commit, and a rig in a rack cannot be asked which one it is
# running.
#
# Two forms come out of the same tag, and they are not interchangeable:
#
#   (default)   the package version:  1.2.3, 1.2.3~rc1, 1.2.3+7.gabc1234
#   --pep440    the Python version:   1.2.3, 1.2.3rc1,  1.2.3+7.gabc1234
#
# `~` is what sorts a pre-release *below* its release for dpkg and rpm -- and it
# is what routes an artifact to `testing` rather than `stable` in the braemons
# archive -- but it is not a character PEP 440 allows, so the wheel cannot wear
# it. `+n.gsha` is a local version in both worlds and sorts *above* the tag it
# is ahead of, which is what a build from an untagged commit should do.
set -eu

form=deb
[ "${1:-}" = "--pep440" ] && form=pep440

if ! described=$(git describe --tags --match 'v[0-9]*' --dirty 2>/dev/null); then
  # No tag reachable at all: a fresh clone of a branch, or a shallow CI checkout
  # that fetched no tags. Say so on stderr and emit something ordered rather
  # than failing the build -- but keep it obviously not a release.
  echo "git-version.sh: no v* tag reachable; this is not a release version" >&2
  sha=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
  echo "0.0.0+g$sha"
  exit 0
fi

# v1.2.3            -> tag=1.2.3  ahead=  sha=      dirty=
# v1.2.3-rc1        -> the -rc1 is part of the tag, not a commit count
# v1.2.3-7-gabc1234 -> ahead=7 sha=abc1234
rest=${described#v}
dirty=
case "$rest" in *-dirty) dirty=.dirty; rest=${rest%-dirty} ;; esac

ahead=
sha=
# git describe appends -<n>-g<sha>; a pre-release tag's own hyphen never has
# that shape, which is what tells the two apart.
case "$rest" in
  *-*-g*)
    sha=${rest##*-g}
    without_sha=${rest%-g*}
    ahead=${without_sha##*-}
    rest=${without_sha%-*}
    ;;
esac

# A pre-release tag wears a hyphen -- v1.2.3-rc1 -- and the two forms spell it
# differently: `~rc1` sorts below 1.2.3 for dpkg and rpm, `rc1` is what PEP 440
# already means by it.
case "$rest" in
  *-*) if [ "$form" = deb ]; then rest="${rest%%-*}~${rest#*-}"
       else rest="${rest%%-*}${rest#*-}"; fi ;;
esac

if [ -n "$ahead" ] || [ -n "$dirty" ]; then
  # Ahead of the tag, or built from a tree that matches no commit. A local
  # version in both worlds, and it sorts above the tag it is ahead of.
  [ -n "$sha" ] || sha=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
  rest="$rest+${ahead:-0}.g$sha$dirty"
fi

echo "$rest"
