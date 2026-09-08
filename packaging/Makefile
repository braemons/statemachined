# The .deb and the .rpm, built the way vstimd builds its own.
#
# Two things this is, and one it is not.
#
# **Vendored.** A self-contained Python tree lands under
# /opt/braemons/statemachined -- interpreter and all -- rather than a set of
# `python3-*` dependencies. That is the braemons pattern (dev/DAEMON.md §6.1)
# and the reason is a rig rather than taste: an OS upgrade that moves Python
# must not be able to stop an experiment, and a box in a rack should not need a
# working pip to be repaired. The price is that a pure-Python package is
# per-architecture.
#
# **Built in a pinned container**, like vstimd's. `packages-amd64` and
# `packages-arm64` build inside packaging/docker/Dockerfile.package-builder,
# whose base image and every tool in it are pinned by digest or version, and
# whose file mtimes come from the commit rather than from the clock. So the
# artifact is a function of the commit and this repository, not of who ran it
# -- which is what makes a CI build and a developer's build comparable.
#
# What it is **not** is a cross-compile. vstimd cross-compiles because it is
# Rust; the only thing compiled here is the native device -- the firmware built
# for the host, which `statemachined device` runs so a box with no board still
# has something real to talk to -- and the vendored interpreter is a native
# artifact besides. So the arm64 package is built by running the builder image
# as arm64 under qemu. Register the handler once per boot:
#
#     docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
#
#     make -C packaging packages-arm64    # the Pi 5, beside vstimd and triald
#     make -C packaging packages          # both architectures, both formats
#     make -C packaging deb               # this machine, no container: fast
#     make -C packaging check             # run the staged tree, package nothing
#
# `make image` in the repository root first, and the flashable firmware goes
# into the package too.

ROOT  := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
BUILD := $(ROOT)/build/package
STAGE := $(BUILD)/root
SRC   := $(BUILD)/src

# vstimd's name for the same thing, and what .github/workflows/release.yml
# collects from.
DIST_DIR ?= $(ROOT)/dist

PACKAGE_BUILDER_IMAGE ?= statemachined-package-builder

# Where the tree lands on the target: the path the launcher's shebang, the
# systemd unit and nfpm.yaml all name. Changing it means changing all three.
PREFIX := /opt/braemons/statemachined

# The interpreter uv fetches from python-build-standalone. Pinned here because
# the package is built out of it: a rig and CI disagreeing about which Python
# ran the daemon is exactly the difference this vendoring exists to remove.
PYTHON_VERSION ?= 3.12

UV   ?= uv
NFPM ?= nfpm
NFPM_PIN := 2.43.1

# ── The version, from the one place it is defined ───────────────────────────
#
# Override to build outside a tagged checkout; the packaging container does
# exactly that, since its build context has no .git:
#
#     make -C packaging deb STATEMACHINED_VERSION=0.1.0
#
# An explicit value short-circuits the script rather than being handed to it.
STATEMACHINED_VERSION ?=
RESOLVED_VERSION := $(strip $(if $(strip $(STATEMACHINED_VERSION)),$(strip $(STATEMACHINED_VERSION)),\
                      $(shell $(ROOT)/packaging/scripts/git-version.sh 2>/dev/null)))
RESOLVED_PY_VERSION := $(strip $(if $(strip $(STATEMACHINED_VERSION)),\
                         $(shell STATEMACHINED_VERSION='$(strip $(STATEMACHINED_VERSION))' \
                                 $(ROOT)/packaging/scripts/git-version.sh --pep440 2>/dev/null),\
                         $(shell $(ROOT)/packaging/scripts/git-version.sh --pep440 2>/dev/null)))

# Missing version = hard stop, never a default: every path below bakes it into
# a filename and into package metadata, so continuing with an invented one
# would produce artifacts that lie about what they are.
#
# Recursive (`=`, not `:=`) so the error fires where VERSION is *used* rather
# than when this file is parsed, which keeps `clean` and `install-nfpm` working
# in a checkout with no tags.
VERSION    = $(or $(RESOLVED_VERSION),$(error Cannot determine the version. Run \
packaging/scripts/git-version.sh to see why, or pass STATEMACHINED_VERSION=<version>))
PY_VERSION = $(or $(RESOLVED_PY_VERSION),$(error Cannot determine the version))

REVISION ?= 1

MAINTAINER ?= Joscha Schmiedt <joscha.schmiedt@gmail.com>

# Every mtime in the package, from the commit rather than from the clock. Two
# builds of one commit otherwise share not a single byte, which makes "did this
# artifact come from that source" unanswerable. nfpm reads this from the
# environment as well; it is passed explicitly so the value is visible here.
# Simply expanded: a recursive one cannot be re-exported under its own name
# below, and would re-run git for every mention besides.
ifndef SOURCE_DATE_EPOCH
SOURCE_DATE_EPOCH := $(shell git -C $(ROOT) log -1 --format=%ct 2>/dev/null || echo 0)
endif
MTIME := $(shell date -u -d @$(SOURCE_DATE_EPOCH) +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
# Exported by name, not re-assigned: `export FOO = $(FOO)` is a variable that
# references itself, which make refuses. nfpm reads it from the environment.
export SOURCE_DATE_EPOCH

# nfpm speaks Go's names for architectures, which are also dpkg's for the two
# that matter. Derived rather than asked for: a package labelled with an
# architecture it was not built on is worse than one that will not build.
UNAME_M := $(shell uname -m)
ARCH := $(strip \
  $(if $(filter x86_64,$(UNAME_M)),amd64, \
  $(if $(filter aarch64 arm64,$(UNAME_M)),arm64, \
  $(if $(filter armv7l,$(UNAME_M)),arm7,))))

# The flashable images, if `make image` has been run in the repository root. A
# package without them still installs; GET /api/device/firmware then has nothing
# to compare the running board against, and says so.
IMAGE_DIR ?= $(ROOT)/image

# Documentation that ships in the package, because the box in the rack is where
# somebody is standing when they need it.
DOCS := README.md LICENSE NOTICE dev/DAEMON.md dev/API.md dev/PROTOCOL.md dev/HARDWARE.md dev/BRINGUP.md

.PHONY: packages-amd64 packages-arm64 packages packages-here \
        deb rpm stage stage-device stage-docs stage-firmware check inspect repro \
        print-version install-nfpm clean help

# ── Built in the pinned container, one per architecture ─────────────────────
#
# The image does the staging and the packing; this hands it the version and the
# timestamp, because its build context has no .git to derive them from. Then a
# container run copies the artifacts out -- vstimd's shape exactly.

packages-amd64:             ## the .deb and .rpm for amd64, in the pinned image
	@$(MAKE) --no-print-directory package-in-container PLATFORM=linux/amd64 TAG=amd64

packages-arm64:             ## the same for arm64 -- the Pi 5 -- under qemu
	@$(MAKE) --no-print-directory package-in-container PLATFORM=linux/arm64 TAG=arm64

packages: packages-amd64 packages-arm64  ## every artifact a release publishes

.PHONY: package-in-container
package-in-container:
	@#  image/ is a COPY source in the Dockerfile, so it has to exist even when
	@#  nobody has run `make image`. An empty one is a package that says so.
	@mkdir -p $(IMAGE_DIR) $(DIST_DIR)
	cd $(ROOT) && DOCKER_BUILDKIT=1 docker build \
	  --platform $(PLATFORM) \
	  -f packaging/docker/Dockerfile.package-builder \
	  --build-arg STATEMACHINED_VERSION=$(VERSION) \
	  --build-arg SOURCE_DATE_EPOCH=$(SOURCE_DATE_EPOCH) \
	  -t $(PACKAGE_BUILDER_IMAGE)-$(TAG) .
	docker run --rm --platform $(PLATFORM) \
	  -v $(abspath $(DIST_DIR)):/output $(PACKAGE_BUILDER_IMAGE)-$(TAG)

# What the container runs. Not for direct use on a dev box -- it assumes the
# image's tools and does no Docker work itself -- but it is the same recipe
# `make deb` uses here, so there is one definition of how a package is built.
packages-here: deb rpm

# ── Built on this machine, for iterating ────────────────────────────────────
#
# Faster than a container round trip and correct for this architecture only.
# What a release publishes comes from `packages` above.

deb: check                  ## the .deb for this machine, no container
	@mkdir -p $(DIST_DIR)
	cd $(ROOT) && $(NFPM) package -f packaging/nfpm.yaml -p deb -t $(DIST_DIR)

rpm: check                  ## the .rpm for this machine, no container
	@mkdir -p $(DIST_DIR)
	cd $(ROOT) && $(NFPM) package -f packaging/nfpm.yaml -p rpm -t $(DIST_DIR)

# What nfpm.yaml reads, attached to the two targets that run nfpm rather than
# exported globally. A global `export FOO = $(VERSION)` is expanded for *every*
# recipe, including `clean` and `install-nfpm`, so an untagged checkout could
# not run any of them without hitting VERSION's error.
#
# SOURCE_DATE_EPOCH is nfpm's own convention for the timestamps it writes; the
# staged tree's file mtimes are set to match in stage-deterministic.
deb rpm: export STATEMACHINED_ARCH        = $(ARCH)
deb rpm: export STATEMACHINED_MAINTAINER  = $(MAINTAINER)
deb rpm: export STATEMACHINED_RELEASE     = $(REVISION)
deb rpm: export STATEMACHINED_PKG_VERSION = $(VERSION)

# ── The staged tree ─────────────────────────────────────────────────────────

stage: $(STAGE)/.stamp      ## build the tree the package is cut from
$(STAGE)/.stamp: $(shell find $(ROOT)/daemon/src $(ROOT)/firmware/core $(ROOT)/firmware/hal \
                              $(ROOT)/firmware/native -type f) \
                 $(ROOT)/daemon/pyproject.toml $(ROOT)/CMakeLists.txt
	@test -n "$(ARCH)" || { \
	  echo "error: no nfpm architecture known for $(UNAME_M)."; \
	  echo "       Build on the target architecture, or add it above."; exit 1; }
	rm -rf $(BUILD)
	@mkdir -p $(STAGE)$(PREFIX) $(SRC) $(DIST_DIR)

	@#  The interpreter, copied out of uv's managed store rather than symlinked
	@#  into it. --managed-python so this cannot silently vendor whatever Python
	@#  happens to be on PATH, which would defeat the entire exercise.
	$(UV) python install --managed-python $(PYTHON_VERSION)
	cp -a "$$(dirname $$(dirname $$($(UV) python find --managed-python $(PYTHON_VERSION))))/." \
	  $(STAGE)$(PREFIX)/
	@#  uv marks its own installs externally managed so that nothing writes into
	@#  the store. This copy is not that store: it is a tree being built into a
	@#  package, and the daemon is about to be installed into it.
	find $(STAGE)$(PREFIX) -name EXTERNALLY-MANAGED -delete

	@#  The daemon, stamped. pyproject.toml carries the 0.0.0 sentinel in the
	@#  repository, so the version is substituted in a copy -- a build must never
	@#  leave the working tree dirty, and a dirty tree is itself part of the
	@#  version string.
	cp -a $(ROOT)/daemon/. $(SRC)/
	rm -rf $(SRC)/tests $(SRC)/bench
	sed -i 's/^version = "0.0.0"$$/version = "$(PY_VERSION)"/' $(SRC)/pyproject.toml
	@grep -q '^version = "$(PY_VERSION)"$$' $(SRC)/pyproject.toml || { \
	  echo "error: the version sentinel in daemon/pyproject.toml did not match."; \
	  echo "       Nothing was stamped, and a 0.0.0 package is a package nobody"; \
	  echo "       can correlate with a commit. Fix the sed above."; exit 1; }
	@#  The dependencies come from daemon/uv.lock, exported with hashes, rather
	@#  than being resolved against PyPI at build time. Otherwise a release of
	@#  fastapi between two builds of one commit changes the artifact, which is
	@#  the difference between "reproducible" and "reproducible this afternoon".
	@#  Exported from the unmodified daemon/ because that is where the lock
	@#  matches its pyproject; the project itself then goes in --no-deps.
	$(UV) export --project $(ROOT)/daemon --frozen --no-default-groups --no-emit-project \
	  --no-header --no-annotate --format requirements.txt -o $(SRC)/requirements.txt
	$(UV) pip install --python $(STAGE)$(PREFIX)/bin/python3 \
	  --link-mode=copy --no-cache --quiet -r $(SRC)/requirements.txt
	$(UV) pip install --python $(STAGE)$(PREFIX)/bin/python3 \
	  --link-mode=copy --no-cache --quiet --no-deps $(SRC)

	@#  Nothing in here is a development environment: no headers, no pip, no
	@#  tkinter, and no console scripts but ours -- the ones uv writes carry the
	@#  *build* machine's path to the interpreter, so shipping them would ship a
	@#  handful of commands that are broken on every target.
	rm -rf $(STAGE)$(PREFIX)/include $(STAGE)$(PREFIX)/share
	rm -rf $(STAGE)$(PREFIX)/lib/python*/idlelib $(STAGE)$(PREFIX)/lib/python*/tkinter \
	       $(STAGE)$(PREFIX)/lib/python*/lib2to3 $(STAGE)$(PREFIX)/lib/python*/ensurepip \
	       $(STAGE)$(PREFIX)/lib/python*/site-packages/pip \
	       $(STAGE)$(PREFIX)/lib/python*/site-packages/pip-*.dist-info
	find $(STAGE)$(PREFIX)/bin -mindepth 1 \
	  -not -name python -not -name python3 -not -name 'python3.[0-9]*' -delete
	rm -f $(STAGE)$(PREFIX)/bin/*-config
	@#  python-build-standalone leaves a BUILD manifest of its own at the root of
	@#  the tree. Harmless, and not something a rig should be asked to explain.
	rm -f $(STAGE)$(PREFIX)/BUILD

	@#  One launcher, written here rather than taken from the wheel, so that the
	@#  shebang is the installed path and not this machine's staging directory.
	@printf '%s\n' \
	  '#!$(PREFIX)/bin/python3' \
	  '# The installed entry point. See packaging/Makefile for why it is not' \
	  '# the console script the wheel carries.' \
	  'import sys' \
	  'from statemachined.command_line_interface import main' \
	  '' \
	  'if __name__ == "__main__":' \
	  '    sys.exit(main())' \
	  > $(STAGE)$(PREFIX)/bin/statemachined
	chmod 0755 $(STAGE)$(PREFIX)/bin/statemachined

	$(MAKE) --no-print-directory stage-device stage-docs stage-firmware stage-deterministic
	@touch $@

# The firmware, compiled for the host, inside the package.
#
# It looks like a test fixture and is not one. A box that has just run
# `apt install braemons-statemachined` has a daemon and no board, and the first
# question anybody asks of it is whether it works -- `statemachined device` on
# one terminal, `-t socket://127.0.0.1:5300` on the other, and the answer
# involves the firmware's own session, engine and result chunker rather than a
# mock of them. dev/DAEMON.md has called this "how anybody runs this daemon with
# no board on the desk" since M4h; until now it was only true from a checkout.
#
# libexec/ rather than bin/: nothing types this name. `statemachined device`
# finds it relative to the interpreter that is running (see
# device/native_device_on_a_socket.py), which is why the path is not written
# down anywhere but here.
#
# A build with no compiler still produces a package. It is not the release
# path -- packaging/docker/Dockerfile.package-builder carries g++ and cmake
# precisely so a released package always has this -- but a developer's
# `make -C packaging deb` on a machine without them should say so and carry on
# rather than fail at the last step.
stage-device:
	@mkdir -p $(STAGE)$(PREFIX)/libexec
	@if command -v cmake >/dev/null 2>&1 && command -v c++ >/dev/null 2>&1; then \
	  cmake -S $(ROOT) -B $(BUILD)/device -DCMAKE_BUILD_TYPE=Release >/dev/null && \
	  cmake --build $(BUILD)/device -j --target statemachined_native_device >/dev/null && \
	  install -m 0755 $(BUILD)/device/statemachined_native_device \
	    $(STAGE)$(PREFIX)/libexec/statemachined-device && \
	  echo "  device: statemachined-device, $$(du -h $(STAGE)$(PREFIX)/libexec/statemachined-device | cut -f1)"; \
	else \
	  printf '%s\n' \
	    'No native device was built into this package.' \
	    '' \
	    'It is the firmware compiled for this machine, and `statemachined device`' \
	    'runs it so that a daemon on a box with no board has something real to' \
	    'talk to. Building it needs cmake and a C++17 compiler, which the machine' \
	    'that packed this did not have.' \
	    '' \
	    'Released packages always carry it -- see' \
	    'packaging/docker/Dockerfile.package-builder. Build one from a checkout' \
	    'with `make -C packaging deb` on a machine with a compiler.' \
	    > $(STAGE)$(PREFIX)/libexec/NO-DEVICE.txt; \
	  echo "  device: none (cmake and a C++ compiler build it)"; \
	fi

stage-docs:
	@mkdir -p $(STAGE)/usr/share/doc/braemons-statemachined
	@for doc in $(DOCS); do cp $(ROOT)/$$doc $(STAGE)/usr/share/doc/braemons-statemachined/; done

# The images if they are there, and an honest note if they are not. nfpm has no
# way to say "this file if it exists", so the directory is always populated with
# something -- and a package that quietly shipped no firmware would be a box
# that cannot answer what it is supposed to be running.
stage-firmware:
	@mkdir -p $(STAGE)/usr/share/braemons/statemachined/firmware
	@if [ -d "$(IMAGE_DIR)" ] && ls $(IMAGE_DIR)/*.bin >/dev/null 2>&1; then \
	  cp $(IMAGE_DIR)/*.bin $(IMAGE_DIR)/*.elf $(IMAGE_DIR)/MANIFEST.txt \
	     $(STAGE)/usr/share/braemons/statemachined/firmware/ 2>/dev/null || true; \
	  echo "  firmware: $$(ls $(IMAGE_DIR)/*.bin | wc -l) image(s) from $(IMAGE_DIR)"; \
	else \
	  printf '%s\n' \
	    'No firmware was built into this package.' \
	    '' \
	    'The images are built from the same repository with `make image`, which' \
	    'writes them and a MANIFEST.txt recording the commit, sizes and' \
	    'checksums -- a board in a rack cannot be asked which commit it is' \
	    'running. Run that before building the package to include them.' \
	    '' \
	    'GET /api/device/firmware has nothing to compare the running board' \
	    'against until it does, and says so rather than guessing.' \
	    > $(STAGE)/usr/share/braemons/statemachined/firmware/NO-FIRMWARE.txt; \
	  echo "  firmware: none (run \`make image\` first to include it)"; \
	fi

# What makes two builds of one commit the same bytes.
#
# Three things drift, and the order below is the fix for all three.
#
# uv leaves a `uv_cache.json` in the dist-info holding the wall-clock time of
# the install, to the nanosecond. It is uv's own bookkeeping and means nothing
# to an installed package, but it drags RECORD and md5sums along with it.
#
# Every mtime in the tree is when the build ran: uv unpacks the interpreter
# fresh each time, so even the stdlib's timestamps are new on every build.
# They all become the commit's instead.
#
# And a .pyc records the mtime of the source it was compiled from, so
# normalising mtimes *invalidates every .pyc python-build-standalone shipped* --
# which would leave the daemon recompiling the stdlib on every start, into a
# tree its user cannot write. So every .pyc in the tree is rebuilt with
# hash-based invalidation instead, `unchecked` because the tree is read-only
# and there is nothing to check against. `-s`/`-p` rewrite the recorded paths
# from this staging directory to the installed one, so a traceback names
# /opt/braemons/... rather than somebody's build directory.
.PHONY: stage-deterministic
stage-deterministic:
	@find $(STAGE)$(PREFIX)/lib/python*/site-packages -name uv_cache.json -delete
	@sed -i '/uv_cache\.json/d' \
	  $(STAGE)$(PREFIX)/lib/python*/site-packages/*.dist-info/RECORD
	@find $(STAGE) -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@find $(STAGE) -exec touch --no-dereference -d @$(SOURCE_DATE_EPOCH) {} +
	@$(STAGE)$(PREFIX)/bin/python3 -m compileall -q -f \
	  --invalidation-mode unchecked-hash \
	  -s $(STAGE) -p / \
	  $(STAGE)$(PREFIX)/lib/python$(PYTHON_VERSION) >/dev/null
	@#  The .pyc files just written carry this moment's mtime; the tar nfpm
	@#  builds records it, so they go back to the commit's too.
	@find $(STAGE) -exec touch --no-dereference -d @$(SOURCE_DATE_EPOCH) {} +
	@echo "  deterministic: mtimes at $(MTIME), hash-based .pyc throughout"

# ── Before it is a package ──────────────────────────────────────────────────

# The staged tree, run. Every other test in this repository runs the daemon out
# of a checkout with uv building the environment; this is the only thing that
# runs the interpreter that is about to be shipped, with the dependencies that
# are about to be shipped, and it is where a missing `uvicorn[standard]` extra
# or a web asset that never made it into the wheel shows up.
#
# PYTHONDONTWRITEBYTECODE, or this would drop fresh mtime-based .pyc files into
# the tree stage-deterministic just finished making reproducible.
check: stage                ## run the staged daemon: imports, entry point, assets
	@PYTHONDONTWRITEBYTECODE=1 $(STAGE)$(PREFIX)/bin/python3 \
	  $(ROOT)/packaging/scripts/check-staged-tree.py
	@#  The launcher cannot be run by its own shebang here: that shebang is the
	@#  *installed* interpreter, which is the whole point of writing it by hand,
	@#  and it does not exist on this machine. So run it through the staged
	@#  python, and check the shebang separately -- the two together are what
	@#  `systemctl start` will do.
	@PYTHONDONTWRITEBYTECODE=1 $(STAGE)$(PREFIX)/bin/python3 \
	  $(STAGE)$(PREFIX)/bin/statemachined --help >/dev/null
	@head -1 $(STAGE)$(PREFIX)/bin/statemachined | grep -qxF '#!$(PREFIX)/bin/python3' \
	  || { echo "error: the launcher's shebang is not $(PREFIX)/bin/python3"; exit 1; }
	@echo "  the launcher runs, and names $(PREFIX)/bin/python3"
	@echo "  version $(VERSION)  (python: $(PY_VERSION))"
	@echo "  staged tree $$(du -sh $(STAGE) | cut -f1), for $(ARCH)"

# What went in, without installing it. `dpkg -c` is the answer to "did the
# conffiles land in /etc and the tree in /opt", which is a question worth
# answering before a Pi does.
inspect:                    ## list what the built packages contain
	@for deb in $(DIST_DIR)/*.deb; do \
	  echo "== $$deb"; dpkg-deb -I "$$deb"; \
	  dpkg-deb -c "$$deb" | awk '{print $$6}' | grep -v '$(PREFIX)/lib' | sort; \
	done

# The reproducibility claim, checked rather than asserted: build, keep the
# checksum, wipe everything, build again, compare. It is a claim worth checking
# because it fails silently -- one file carrying a wall-clock timestamp is
# enough, and the artifact still installs perfectly.
.PHONY: repro
repro:                      ## build the .deb twice and prove the bytes match
	@$(MAKE) --no-print-directory clean >/dev/null
	@$(MAKE) --no-print-directory deb >/dev/null
	@first=$$(sha256sum $(DIST_DIR)/*.deb | cut -d' ' -f1); \
	 $(MAKE) --no-print-directory clean >/dev/null; \
	 $(MAKE) --no-print-directory deb >/dev/null; \
	 second=$$(sha256sum $(DIST_DIR)/*.deb | cut -d' ' -f1); \
	 if [ "$$first" = "$$second" ]; then \
	   echo "reproducible: $$first"; \
	 else \
	   echo "NOT reproducible:"; echo "  $$first"; echo "  $$second"; \
	   echo "  Compare with: diffoscope, or ar x both and diff the tars."; \
	   exit 1; \
	 fi

# Fails visibly when the tag situation leaves the build without a version,
# rather than aborting deep inside a Docker build. The release workflow's first
# step.
print-version:              ## the version this checkout would build
	@echo $(VERSION)

install-nfpm:               ## the pinned nfpm, into ~/.local/bin
	@mkdir -p $(HOME)/.local/bin
	@case "$(UNAME_M)" in \
	  x86_64)  a=x86_64 ;; \
	  aarch64) a=arm64  ;; \
	  *) echo "no nfpm build for $(UNAME_M)" >&2; exit 1 ;; \
	esac; \
	curl -fsSL -o /tmp/nfpm.tar.gz \
	  "https://github.com/goreleaser/nfpm/releases/download/v$(NFPM_PIN)/nfpm_$(NFPM_PIN)_Linux_$$a.tar.gz"
	tar xzf /tmp/nfpm.tar.gz -C $(HOME)/.local/bin nfpm
	rm -f /tmp/nfpm.tar.gz
	@$(HOME)/.local/bin/nfpm --version | head -2

clean:
	rm -rf $(BUILD) $(DIST_DIR)

help:
	@grep -E '^[a-z][a-z0-9-]*:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
