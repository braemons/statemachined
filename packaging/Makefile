# The .deb, and the .rpm that costs one more line.
#
# What this builds is a **vendored** package: a self-contained Python tree under
# /opt/braemons/statemachined, not a set of `python3-*` dependencies. That is
# the braemons pattern (dev/DAEMON.md §6.1) and the reason is a rig rather than
# taste -- an OS upgrade that moves Python must not be able to stop an
# experiment, and a box in a rack should not need a working pip to be fixed. The
# price is that the packages are per-architecture despite being pure Python.
#
# One nfpm.yaml describes both formats. Everything below just gets the staged
# tree into the shape that file expects and hands nfpm a version.
#
#     make -C packaging deb          the package for this machine
#     make -C packaging packages     .deb and .rpm
#     make -C packaging check        run the staged daemon before packing it
#
# Cross-building is deliberately not attempted. The interpreter is a native
# artifact, so an arm64 package is built on an arm64 machine -- a Pi, or a
# container on one. `make deb` refuses to guess.

ROOT  := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
BUILD := $(ROOT)/build/package
STAGE := $(BUILD)/root
SRC   := $(BUILD)/src
DIST  := $(BUILD)/dist

# Where the tree lands on the target, and the path the launcher's shebang and
# the systemd unit both name. Changing it means changing both.
PREFIX := /opt/braemons/statemachined

# The interpreter uv fetches from python-build-standalone. Pinned here because
# the packages are built from it: a rig and CI disagreeing about which Python
# ran the daemon is exactly the class of difference this vendoring exists to
# remove.
PYTHON_VERSION ?= 3.12

UV   ?= uv
NFPM ?= nfpm

# Pinned like every other tool in this repository. See install-nfpm.
NFPM_PIN := 2.43.1

# From the tag, never by hand -- daemon/pyproject.toml carries the 0.0.0
# sentinel. The two forms are not interchangeable; scripts/git-version.sh says
# why.
# Simply expanded, and guarded rather than `?=`: a recursive variable would run
# git describe -- and print its warning -- once for every place it is mentioned.
# An override from the command line or the environment still wins.
ifndef VERSION
VERSION := $(shell $(ROOT)/packaging/scripts/git-version.sh)
endif
ifndef PY_VERSION
PY_VERSION := $(shell $(ROOT)/packaging/scripts/git-version.sh --pep440)
endif

MAINTAINER ?= Joscha Schmiedt <joscha.schmiedt@gmail.com>

# nfpm speaks Go's names for architectures, which are also dpkg's for the two
# that matter. Derived rather than asked for, because a package labelled with an
# architecture it was not built on is worse than one that will not build.
UNAME_M := $(shell uname -m)
ARCH := $(strip \
  $(if $(filter x86_64,$(UNAME_M)),amd64, \
  $(if $(filter aarch64 arm64,$(UNAME_M)),arm64, \
  $(if $(filter armv7l,$(UNAME_M)),arm7,))))

# The flashable images, if `make image` has been run. A package without them
# still installs; GET /api/device/firmware then has nothing to compare the
# running board against, and says so.
IMAGE_DIR ?= $(ROOT)/image

# Documentation that ships with the package, because the box in the rack is
# where somebody is standing when they need it.
DOCS := README.md LICENSE NOTICE dev/DAEMON.md dev/API.md dev/PROTOCOL.md dev/HARDWARE.md dev/BRINGUP.md

export STATEMACHINED_VERSION    = $(VERSION)
export STATEMACHINED_ARCH       = $(ARCH)
export STATEMACHINED_STAGE      = $(STAGE)
export STATEMACHINED_MAINTAINER = $(MAINTAINER)

.PHONY: deb
deb: check                  ## the .deb for this machine, into build/package/dist
	@mkdir -p $(DIST)
	cd $(ROOT) && $(NFPM) package -f packaging/nfpm.yaml -p deb -t $(DIST)
	@ls -lh $(DIST)/*.deb

.PHONY: rpm
rpm: check                  ## the .rpm for this machine
	@mkdir -p $(DIST)
	cd $(ROOT) && $(NFPM) package -f packaging/nfpm.yaml -p rpm -t $(DIST)
	@ls -lh $(DIST)/*.rpm

.PHONY: packages
packages: deb rpm           ## both formats from the one staged tree

# --------------------------------------------------------------------------
# The staged tree
# --------------------------------------------------------------------------

.PHONY: stage
stage: $(STAGE)/.stamp      ## build the tree the package is cut from
$(STAGE)/.stamp: $(shell find $(ROOT)/daemon/src -type f) $(ROOT)/daemon/pyproject.toml
	@test -n "$(ARCH)" || { \
	  echo "error: no nfpm architecture known for $(UNAME_M)."; \
	  echo "       Build on the target architecture, or add it above."; exit 1; }
	rm -rf $(BUILD)
	@mkdir -p $(STAGE)$(PREFIX) $(SRC) $(DIST)

	@# The interpreter, copied out of uv's managed store rather than symlinked
	@# into it. --managed-python so this cannot silently vendor whatever Python
	@# happens to be on PATH, which would defeat the entire exercise.
	$(UV) python install --managed-python $(PYTHON_VERSION)
	cp -a "$$(dirname $$(dirname $$($(UV) python find --managed-python $(PYTHON_VERSION))))/." \
	  $(STAGE)$(PREFIX)/
	@# uv marks its own installs externally managed so that nothing writes into
	@# the store. This copy is not that store: it is a tree being built into a
	@# package, and the daemon is about to be installed into it.
	find $(STAGE)$(PREFIX) -name EXTERNALLY-MANAGED -delete

	@# The daemon, stamped. pyproject.toml carries the 0.0.0 sentinel in the
	@# repository, so the version is substituted in a copy -- a build must never
	@# leave the working tree dirty, and a dirty tree is itself part of the
	@# version string.
	cp -a $(ROOT)/daemon/. $(SRC)/
	rm -rf $(SRC)/tests $(SRC)/bench
	sed -i 's/^version = "0.0.0"$$/version = "$(PY_VERSION)"/' $(SRC)/pyproject.toml
	@grep -q '^version = "$(PY_VERSION)"$$' $(SRC)/pyproject.toml || { \
	  echo "error: the version sentinel in daemon/pyproject.toml did not match."; \
	  echo "       Nothing was stamped, and a 0.0.0 package is a package nobody"; \
	  echo "       can correlate with a commit. Fix the sed above."; exit 1; }
	$(UV) pip install --python $(STAGE)$(PREFIX)/bin/python3 \
	  --link-mode=copy --no-cache --quiet $(SRC)

	@# Nothing in here is a development environment: no headers, no pip, no
	@# tkinter, and no console scripts but ours -- the ones uv writes carry the
	@# *build* machine's path to the interpreter, so shipping them would ship a
	@# handful of commands that are broken on every target.
	rm -rf $(STAGE)$(PREFIX)/include $(STAGE)$(PREFIX)/share
	rm -rf $(STAGE)$(PREFIX)/lib/python*/idlelib $(STAGE)$(PREFIX)/lib/python*/tkinter \
	       $(STAGE)$(PREFIX)/lib/python*/lib2to3 $(STAGE)$(PREFIX)/lib/python*/ensurepip \
	       $(STAGE)$(PREFIX)/lib/python*/site-packages/pip \
	       $(STAGE)$(PREFIX)/lib/python*/site-packages/pip-*.dist-info
	find $(STAGE)$(PREFIX)/bin -mindepth 1 \
	  -not -name python -not -name python3 -not -name 'python3.[0-9]*' -delete
	rm -f $(STAGE)$(PREFIX)/bin/*-config
	@# python-build-standalone leaves a BUILD manifest of its own at the root of
	@# the tree. Harmless, and not something a rig should be asked to explain.
	rm -f $(STAGE)$(PREFIX)/BUILD

	@# One launcher, written here rather than taken from the wheel, so that the
	@# shebang is the installed path and not this machine's staging directory.
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

	$(MAKE) --no-print-directory stage-docs stage-firmware
	@touch $@

.PHONY: stage-docs
stage-docs:
	@mkdir -p $(STAGE)/usr/share/doc/statemachined
	@for doc in $(DOCS); do cp $(ROOT)/$$doc $(STAGE)/usr/share/doc/statemachined/; done

# The images if they are there, and an honest note if they are not. nfpm has no
# way to say "this file if it exists", so the directory is always populated with
# something -- and a package that quietly shipped no firmware would be a box
# that cannot answer what it is supposed to be running.
.PHONY: stage-firmware
stage-firmware:
	@mkdir -p $(STAGE)/usr/share/braemons/statemachined/firmware
	@if [ -d "$(IMAGE_DIR)" ] && ls $(IMAGE_DIR)/*.bin >/dev/null 2>&1; then \
	  cp $(IMAGE_DIR)/* $(STAGE)/usr/share/braemons/statemachined/firmware/; \
	  echo "firmware: $$(ls $(IMAGE_DIR)/*.bin | wc -l) image(s) from $(IMAGE_DIR)"; \
	else \
	  printf '%s\n' \
	    'No firmware was built into this package.' \
	    '' \
	    'The images are built from the same repository with `make image`, which' \
	    'writes them and a MANIFEST.txt recording the commit, sizes and' \
	    'checksums -- a board in a rack cannot be asked which commit it is' \
	    'running. Run that before `make -C packaging deb` to include them.' \
	    '' \
	    'GET /api/device/firmware has nothing to compare the running board' \
	    'against until it does, and says so rather than guessing.' \
	    > $(STAGE)/usr/share/braemons/statemachined/firmware/NO-FIRMWARE.txt; \
	  echo "firmware: none (run \`make image\` first to include it)"; \
	fi

# --------------------------------------------------------------------------
# Before it is a package
# --------------------------------------------------------------------------

# The staged tree, run. Every other test in this repository runs the daemon out
# of a checkout with uv building the environment; this is the only thing that
# runs the interpreter that is about to be shipped, with the dependencies that
# are about to be shipped, and it is where a missing `uvicorn[standard]` extra
# or a web asset that never made it into the wheel shows up.
.PHONY: check
check: stage                ## run the staged daemon: imports, entry point, assets
	@$(STAGE)$(PREFIX)/bin/python3 $(ROOT)/packaging/scripts/check-staged-tree.py
	@# The launcher cannot be run by its own shebang here: that shebang is the
	@# *installed* interpreter, which is the whole point of writing it by hand,
	@# and it does not exist on this machine. So run it through the staged
	@# python, and check the shebang separately -- the two together are what
	@# `systemctl start` will do.
	@$(STAGE)$(PREFIX)/bin/python3 $(STAGE)$(PREFIX)/bin/statemachined --help >/dev/null
	@head -1 $(STAGE)$(PREFIX)/bin/statemachined | grep -qxF '#!$(PREFIX)/bin/python3' \
	  || { echo "error: the launcher's shebang is not $(PREFIX)/bin/python3"; exit 1; }
	@echo "  the launcher runs, and names $(PREFIX)/bin/python3"
	@case "$(VERSION)" in \
	  0.0.0*) echo "  warning: $(VERSION) -- no v* tag is reachable, so this is not a release" ;; \
	  *) echo "  version $(VERSION)  (python: $(PY_VERSION))" ;; esac
	@echo "  staged tree $$(du -sh $(STAGE) | cut -f1), for $(ARCH)"

# What went in, without installing it. `dpkg -c` is the answer to "did the
# conffiles land in /etc and the tree in /opt", which is a question worth
# answering before a Pi does.
.PHONY: inspect
inspect:                    ## list what the built .deb contains
	@for deb in $(DIST)/*.deb; do \
	  echo "== $$deb"; dpkg-deb -I "$$deb"; \
	  dpkg-deb -c "$$deb" | awk '{print $$6}' | grep -v '$(PREFIX)/lib' | sort; \
	done

# --------------------------------------------------------------------------

.PHONY: install-nfpm
install-nfpm:               ## the pinned nfpm, into ~/.local/bin
	@mkdir -p $(HOME)/.local/bin
	curl -fsSL -o /tmp/nfpm.tar.gz \
	  "https://github.com/goreleaser/nfpm/releases/download/v$(NFPM_PIN)/nfpm_$(NFPM_PIN)_Linux_$(UNAME_M).tar.gz"
	tar xzf /tmp/nfpm.tar.gz -C $(HOME)/.local/bin nfpm
	rm -f /tmp/nfpm.tar.gz
	@$(HOME)/.local/bin/nfpm --version | head -2

.PHONY: clean
clean:
	rm -rf $(BUILD)

.PHONY: help
help:
	@grep -E '^[a-z][a-z-]*:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
