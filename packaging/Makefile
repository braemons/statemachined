# The .deb and the .rpm, built the way vstimd builds its own.
#
# **One binary.** The daemon is Rust, and the panels are embedded in it, so the
# package is /usr/bin/statemachined, the flashable firmware, the docs, and the
# unit files. No interpreter and no
# dependencies to vendor.
#
# **Built in a pinned container**, like vstimd's. `packages-amd64` and
# `packages-arm64` build inside packaging/docker/Dockerfile.package-builder,
# whose base image, toolchain and every tool in it are pinned, and whose file
# mtimes and embedded paths come from the commit rather than from the clock or
# the machine. So the artifact is a function of the commit and this repository,
# not of who ran it -- which is what makes a CI build and a developer's build
# comparable.
#
# The arm64 package is built by running the builder image as arm64 under qemu
# rather than by cross-compiling, so one recipe serves both. Register the
# handler once per boot:
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

# Where the binary lands on the target: the path the systemd unit and
# nfpm.yaml both name. Changing it means changing both.
BINARY := /usr/bin/statemachined

CARGO ?= cargo
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
# The same version as pip spells it, for the client's wheel.
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
# package without them still installs; the daemon then has nothing to compare
# the running board against, and says so.
IMAGE_DIR ?= $(ROOT)/image

# Documentation that ships in the package, because the box in the rack is where
# somebody is standing when they need it.
DOCS := README.md LICENSE NOTICE docs/developer/daemon.md docs/reference/api.md docs/reference/protocol.md docs/operations/hardware.md docs/operations/bringup.md

.PHONY: packages-amd64 packages-arm64 packages packages-here \
        deb rpm stage stage-docs stage-firmware check inspect repro \
        wheel print-version install-nfpm clean help

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
# The panels are a prerequisite because they are embedded, minus their build
# inputs: `node_modules` is tens of thousands of files.
$(STAGE)/.stamp: $(shell find $(ROOT)/daemon-rs -type f -not -path '*/target/*') \
                 $(shell find $(ROOT)/client/web -type f \
                           -not -path '*/node_modules/*' -not -path '*/gen/*') \
                 $(ROOT)/Cargo.toml $(ROOT)/Cargo.lock
	@test -n "$(ARCH)" || { \
	  echo "error: no nfpm architecture known for $(UNAME_M)."; \
	  echo "       Build on the target architecture, or add it above."; exit 1; }
	rm -rf $(BUILD)
	@mkdir -p $(dir $(STAGE)$(BINARY)) $(DIST_DIR)

	@#  The version is stamped in at compile time, from the one place it is
	@#  defined, rather than written into Cargo.toml: a build must never leave
	@#  the working tree dirty, and a dirty tree is itself part of the version.
	@#
	@#  --remap-path-prefix, so the paths the debug info and panic messages
	@#  carry are the same on every machine: two builds of one commit in two
	@#  checkouts are otherwise two different binaries. --locked, so the
	@#  dependencies are Cargo.lock's and nothing a registry published since.
	cd $(ROOT) && STATEMACHINED_VERSION=$(VERSION) \
	  RUSTFLAGS="--remap-path-prefix=$(ROOT)=/build/statemachined --remap-path-prefix=$(CARGO_HOME_DIR)=/cargo" \
	  $(CARGO) build --release --locked --target-dir $(BUILD)/target \
	    --manifest-path daemon-rs/Cargo.toml
	install -m 0755 $(BUILD)/target/release/statemachined $(STAGE)$(BINARY)

	$(MAKE) --no-print-directory stage-docs stage-firmware stage-deterministic
	@touch $@

# Where cargo keeps the registry's sources, which the debug info names.
CARGO_HOME_DIR := $(or $(CARGO_HOME),$(HOME)/.cargo)

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

# What makes two builds of one commit the same bytes: every mtime in the tree
# becomes the commit's. The binary's own bytes are settled at compile time, by
# --remap-path-prefix above and by the lock.
.PHONY: stage-deterministic
stage-deterministic:
	@find $(STAGE) -exec touch --no-dereference -d @$(SOURCE_DATE_EPOCH) {} +
	@echo "  deterministic: mtimes at $(MTIME)"

# ── Before it is a package ──────────────────────────────────────────────────

# The staged binary, run: its command line, its version, and the panels it
# serves -- which are embedded, so a panel missing from the build shows up here
# and not on a rig. On a loopback port and a scratch store, with no board.
CHECK_PORT ?= 18681
check: stage                ## run the staged daemon: its version and its panels
	@$(STAGE)$(BINARY) --help >/dev/null
	@scratch=$$(mktemp -d); \
	 $(STAGE)$(BINARY) serve --no-mdns --port $(CHECK_PORT) \
	   --config $$scratch/absent.toml --storage-dir $$scratch --device loop:// \
	   >$$scratch/log 2>&1 & daemon=$$!; \
	 for _ in $$(seq 50); do \
	   curl -fsS http://127.0.0.1:$(CHECK_PORT)/ >$$scratch/index.html 2>/dev/null && break; \
	   sleep 0.1; \
	 done; \
	 kill $$daemon; wait $$daemon 2>/dev/null; \
	 grep -q '<html' $$scratch/index.html || { \
	   echo "error: the staged daemon did not serve its panels"; cat $$scratch/log; exit 1; }; \
	 rm -rf $$scratch
	@echo "  the staged daemon serves its panels"
	@echo "  version $(VERSION)"
	@echo "  staged tree $$(du -sh $(STAGE) | cut -f1), for $(ARCH)"

# What went in, without installing it. `dpkg -c` is the answer to "did the
# conffiles land in /etc and the binary in /usr/bin", which is a question worth
# answering before a Pi does.
inspect:                    ## list what the built packages contain
	@for deb in $(DIST_DIR)/*.deb; do \
	  echo "== $$deb"; dpkg-deb -I "$$deb"; \
	  dpkg-deb -c "$$deb" | awk '{print $$6}' | sort; \
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

# ── The client's wheel ──────────────────────────────────────────────────────
#
# What a script, triald and the family's e2e suite import to talk to the
# daemon: `client/python/`, a distribution of its own. Architecture-independent,
# so one serves every rig. Built from a stamped copy because its pyproject
# carries the 0.0.0 sentinel and a build must never leave the tree dirty.
wheel:                      ## the client's wheel, stamped, into dist/
	@test -n "$(RESOLVED_PY_VERSION)" || { echo "no version: see print-version"; exit 1; }
	@rm -rf $(BUILD)/client && mkdir -p $(BUILD)/client $(DIST_DIR)
	tar -C $(ROOT)/client/python --exclude=.venv --exclude=build --exclude=dist \
	  --exclude=__pycache__ --exclude=.pytest_cache -cf - . | tar -C $(BUILD)/client -xf -
	cd $(BUILD)/client && uv version --frozen $(RESOLVED_PY_VERSION) >/dev/null && \
	  uv build --wheel --out-dir $(DIST_DIR)

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
