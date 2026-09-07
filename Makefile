# The core is plain C++17 and builds on the host, so the fastest feedback loop
# in the repo needs no board attached. Use it before reaching for anything else.
#
# This file is the task runner, and .github/workflows/ci.yml calls these targets
# rather than repeating the commands. That is not tidiness: every command CI ran
# and this file did not was a command nobody could reproduce locally, and every
# pinned version written down twice was a pin that drifted. `make ci` runs what
# CI runs. If a job here needs a flag, it goes in this file and CI inherits it.

BUILD ?= build

.PHONY: test
test: check-core            ## build and run the core unit tests
	cmake -S . -B $(BUILD) -DCMAKE_BUILD_TYPE=Debug
	cmake --build $(BUILD) -j
	ctest --test-dir $(BUILD) --output-on-failure

.PHONY: check-core
check-core:                 ## enforce the portable core's constraints
	@./tools/check-core-purity.sh

# Exported rather than set per-recipe so a local run fails the same way CI does:
# a leak or an unsigned overflow should stop the run, not scroll past.
export UBSAN_OPTIONS ?= halt_on_error=1:print_stacktrace=1
export ASAN_OPTIONS  ?= detect_leaks=1

.PHONY: sanitize
sanitize:                   ## the same tests under ASan and UBSan
	cmake -S . -B build-san -DCMAKE_BUILD_TYPE=Debug -DSTATEMACHINED_SANITIZE=ON
	cmake --build build-san -j
	ctest --test-dir build-san --output-on-failure

# The reproducibility claim is that a seed gives the same durations everywhere.
# Optimisation level is the cheapest way to catch a dependence on undefined
# behaviour or on float; the boards are checked on hardware at M3 and M6.
.PHONY: golden
golden:                     ## the tests at -O0 and -O3, for reproducibility
	@for opt in -O0 -O3; do \
	  echo "== $$opt =="; \
	  cmake -S . -B build$$opt -DCMAKE_BUILD_TYPE=Debug -DCMAKE_CXX_FLAGS="$$opt" || exit 1; \
	  cmake --build build$$opt -j || exit 1; \
	  ctest --test-dir build$$opt --output-on-failure || exit 1; \
	done

BOARD ?= uno_r4_minima

.PHONY: firmware
firmware:                   ## build for the reference board
	pio run -e $(BOARD)

# Emulation. Covers what the host build cannot compile -- the pin map, the port
# registers, the timer ISR, the protocol over a real UART -- and says nothing
# about timing. See emulation/README.md.
RENODE_VERSION ?= 1.16.1

.PHONY: emulate
emulate:                    ## run the firmware under Renode, in Robot tests
	pio run -e $(BOARD)_sci
	renode-test emulation/tests/statemachined.robot

.PHONY: upload
upload:                     ## flash the reference board
	pio run -e $(BOARD) -t upload

# The bench instrument dev/BRINGUP.md §4 and §5 ask for. Its one dependency
# (pyserial) lives in daemon/pyproject.toml rather than in whichever
# python3 is on PATH, so `uv run --project` builds an environment for it on
# first use and neither renode-test's interpreter nor the uv-tool sandboxes
# above notice. TARGET is a device path, a host:port, or any pyserial URL --
# the same tool reaches a board on a network as reaches one on a cable.
TARGET ?= /dev/ttyACM0

.PHONY: bringup
bringup:                    ## talk to a board: make bringup ARGS="state"
	uv run --project daemon statemachined -t $(TARGET) $(ARGS)

# The firmware's own session and engine, built for this machine. Named by the
# integration tests' skip message and by the bench, both of which are useless
# without it, and it is a fraction of `make test`: one binary, no ctest.
.PHONY: integration-device
integration-device:         ## build build/statemachined_native_device on its own
	cmake -S . -B $(BUILD) -DCMAKE_BUILD_TYPE=Debug
	cmake --build $(BUILD) -j --target statemachined_native_device

# The bench: the daemon, its API and its web UI, in front of a device. This is
# the target that answers "does the thing work" without a package, a Pi, or
# triald -- open http://127.0.0.1:8081/ and click.
#
# TARGET is the same variable `make bringup` uses, so the same two paths work:
# a board on a cable, or `make bench-device` in another terminal and
# TARGET=socket://127.0.0.1:5300. Greeting a board takes the rig: one that was
# arming its own trials stops doing so until it is told to again.
#
# The stores are seeded from graphs/ and configs/ rather than pointed at them:
# deleting a graph or a state-machine config in the web UI must not delete an
# example from the repository. Copied only when absent, so an edit made on the
# bench survives the next `make bench`.
BENCH_CONFIG    ?= daemon/bench/statemachined_bench_rig_config.toml
BENCH_STORE     := build/bench/graphs
BENCH_CONFIGS   := build/bench/configs
BENCH_HOST      ?= 127.0.0.1
BENCH_PORT      ?= 8081

.PHONY: bench
bench:                      ## the daemon + web UI against a device: make bench TARGET=...
	@mkdir -p $(BENCH_STORE) $(BENCH_CONFIGS) build/bench/trace build/bench/recordings
	@for graph in graphs/*.json; do \
	  [ -f "$(BENCH_STORE)/$$(basename $$graph)" ] || cp "$$graph" $(BENCH_STORE)/; \
	done
	@for config in configs/*.config.json; do \
	  [ -f "$(BENCH_CONFIGS)/$$(basename $$config)" ] || cp "$$config" $(BENCH_CONFIGS)/; \
	done
	uv run --project daemon statemachined -t $(TARGET) serve \
	  --config $(BENCH_CONFIG) \
	  --host $(BENCH_HOST) --port $(BENCH_PORT) $(ARGS)

# The other half of the no-board path: the firmware's own session and engine,
# built for this machine, on a TCP port the daemon can dial. Not a mock -- see
# the file's docstring, and daemon/tests/integration/conftest.py, which is the
# same bridge.
.PHONY: bench-device
bench-device: integration-device  ## the native device on socket://127.0.0.1:5300
	python3 daemon/bench/native_device_on_a_socket.py $(ARGS)

# The only tests in this repository that need hardware. Everything else -- the
# core on the host, the HAL under Renode -- runs in CI with no board attached,
# and neither can answer what this does: the achieved scan rate, what a command
# costs the scan, a drawn duration against a real clock, and a predicate driven
# from real pins.
#
# Deliberately NOT part of `make ci`. A target that fails on every machine
# without a board attached is a target people learn to ignore.
#
# Greeting the board takes the rig, which this cannot avoid: the greeting is
# what hands over. A board that was running on its own stops.
.PHONY: test-hardware
test-hardware:              ## the suite that needs a board: make test-hardware TARGET=...
	uv run --project daemon --group test \
	  pytest daemon/tests/hardware --target=$(TARGET) $(ARGS)

# The daemon's tests that need no board, part of `make ci`.
#
# tests/unit is arithmetic and translation -- the framing, which now exists
# three times in this tree and would otherwise drift silently, and the compiler,
# checked message by message against dev/PROTOCOL.md.
#
# tests/integration drives whole sessions against build/statemachined_native_device,
# which is the firmware's own session and engine built for this machine. It
# skips itself when that binary is not there, so this target is safe to run
# before `make test`; `make ci` runs `make test` first, so in CI it is always
# built.
.PHONY: test-daemon
test-daemon:                ## the daemon's tests that need no board
	uv run --project daemon --group test pytest daemon/tests/unit daemon/tests/integration $(ARGS)

# The two together, in the order that makes the integration half actually run.
.PHONY: test-integration
test-integration: test      ## build the native device, then drive whole sessions against it
	uv run --project daemon --group test pytest daemon/tests/integration $(ARGS)

# Pinned to match .github/workflows/ci.yml. clang-format's output changes
# between major versions, and `BasedOnStyle: Google` in .clang-format resolves
# against whichever version is running, so an unpinned one reformats files CI
# then rejects. Install it with uv (see install-clang-format), not from a
# package manager.
CLANG_FORMAT      ?= clang-format
CLANG_FORMAT_PIN  := 23.1.0

SOURCES = $(shell find firmware tests -name '*.cpp' -o -name '*.h' | grep -v third_party)

.PHONY: check-clang-format
check-clang-format:
	@have=$$($(CLANG_FORMAT) --version 2>/dev/null \
	  | sed -n 's/.*clang-format version \([0-9.]*\).*/\1/p'); \
	if [ -z "$$have" ]; then \
	  echo "error: $(CLANG_FORMAT) not found."; \
	  echo "       pip install clang-format==$(CLANG_FORMAT_PIN)   (see BUILD.md)"; \
	  exit 1; \
	elif [ "$$have" != "$(CLANG_FORMAT_PIN)" ]; then \
	  echo "error: clang-format $$have, but this repo pins $(CLANG_FORMAT_PIN)."; \
	  echo "       Versions disagree on output, so formatting with $$have can"; \
	  echo "       produce a diff CI rejects."; \
	  echo "       pip install clang-format==$(CLANG_FORMAT_PIN)   (see BUILD.md)"; \
	  echo "       To override anyway: make format CLANG_FORMAT_PIN=$$have"; \
	  exit 1; \
	fi

.PHONY: format
format: check-clang-format  ## clang-format every source file in place
	$(CLANG_FORMAT) -i $(SOURCES)

.PHONY: format-check
format-check: check-clang-format  ## verify formatting the way CI does, changing nothing
	$(CLANG_FORMAT) --dry-run --Werror $(SOURCES)

# --------------------------------------------------------------------------
# Toolchain, pinned in one place
# --------------------------------------------------------------------------
#
# CI calls these instead of carrying its own copy of each version. A pin
# written down twice is a pin that drifts, and the failure mode is a CI job
# rejecting output no contributor can reproduce.
#
# The Python tools go in via `uv tool install`, not the system pip: uv gives
# each one its own isolated environment with a shim on PATH, so this works the
# same on a PEP-668 "externally managed" distro as in CI, and touches nothing
# apt owns. Pinned to the same versions .devcontainer/Dockerfile bakes in.

PLATFORMIO_PIN := 6.1.16

.PHONY: install-pio
install-pio:                ## PlatformIO, for the board and emulation builds
	uv tool install platformio==$(PLATFORMIO_PIN)

.PHONY: install-clang-format
install-clang-format:       ## the pinned clang-format, via uv not the distro
	uv tool install clang-format==$(CLANG_FORMAT_PIN)

# The Robot keyword library ships inside Renode, so a different Renode is a
# different set of keywords. Pinned to the devcontainer's version.
RENODE_URL = https://github.com/renode/renode/releases/download/v$(RENODE_VERSION)/renode-$(RENODE_VERSION).linux-portable-dotnet.tar.gz

.PHONY: install-renode
install-renode:             ## the pinned Renode, portable, into /opt/renode
	curl -fsSL $(RENODE_URL) -o /tmp/renode.tar.gz
	mkdir -p /opt/renode
	sudo tar xzf /tmp/renode.tar.gz -C /opt/renode --strip-components=1
	sudo ln -sf /opt/renode/renode-test /usr/local/bin/renode-test
	# renode-test drives Robot Framework from whichever python3 is on PATH, so
	# these have to land in that interpreter's site-packages rather than in a
	# uv-tool sandbox -- and that directory is root-owned, hence the sudo. uv is
	# not on root's PATH, so hand it over explicitly.
	sudo env "PATH=$$PATH" uv pip install --system --break-system-packages \
		-r /opt/renode/tests/requirements.txt

# --------------------------------------------------------------------------
# A flashable image
# --------------------------------------------------------------------------
#
# One image, since the demo paradigm left: there used to be a bench build that
# ran a graph compiled into the firmware and a rig build that compiled it out,
# and flashing the wrong one was a thing somebody was going to do. A bench board
# now runs a real uploaded graph out of its own storage like any other, so there
# is one binary and one thing to flash. What it is still travels with it -- a
# board in a rack cannot be asked which commit it is running.
IMAGE_DIR ?= image
IMAGE_SHA ?= $(shell git rev-parse HEAD 2>/dev/null || echo unknown)

.PHONY: image
image:                      ## build the flashable image, with a manifest
	rm -rf $(IMAGE_DIR)
	mkdir -p $(IMAGE_DIR)
	$(MAKE) firmware
	cp .pio/build/$(BOARD)/firmware.bin $(IMAGE_DIR)/statemachined-$(BOARD).bin
	cp .pio/build/$(BOARD)/firmware.elf $(IMAGE_DIR)/statemachined-$(BOARD).elf
	@{ \
	  echo "statemachined firmware for the $(BOARD)"; \
	  echo; \
	  echo "commit: $(IMAGE_SHA)"; \
	  echo "built:  $$(date -u +%Y-%m-%dT%H:%M:%SZ)"; \
	  echo "pio:    $$(pio --version)"; \
	  echo; \
	  echo "statemachined-$(BOARD).bin"; \
	  echo "  Holds no graph until one is uploaded, and comes back up running"; \
	  echo "  whatever it was last saved with. Wiring in dev/HARDWARE.md"; \
	  echo; \
	  echo "flash with:  make upload   or   bossac -i -e -w -R <file>.bin"; \
	  echo; \
	  echo "sizes:"; \
	  stat -c '  %n  %s bytes' $(IMAGE_DIR)/*.bin | sed 's|$(IMAGE_DIR)/||'; \
	  echo; \
	  echo "sha256:"; \
	  sha256sum $(IMAGE_DIR)/*.bin $(IMAGE_DIR)/*.elf | sed 's|$(IMAGE_DIR)/|  |'; \
	} > $(IMAGE_DIR)/MANIFEST.txt
	@cat $(IMAGE_DIR)/MANIFEST.txt

# --------------------------------------------------------------------------
# The installable package
# --------------------------------------------------------------------------
#
# `.deb` and `.rpm` for a rig: a vendored interpreter under
# /opt/braemons/statemachined, a systemd unit, a udev rule naming the board, a
# conffile describing the box, and the flashable firmware `make image` builds.
# It lives in packaging/ rather than here because it is a build of its own --
# see packaging/README.md and dev/DAEMON.md §6 -- and these lines exist so that
# nobody has to know that to build one.
#
# `make packages` is what a release publishes: both architectures, each built
# inside a pinned container, so the artifact is a function of the commit rather
# than of the machine. `make deb` is the quick one for iterating -- this
# architecture, no container.
#
# `make image` first, and the firmware goes into the package. Without it the
# package installs a note saying why there is none.

.PHONY: deb
deb:                        ## the .deb for this machine, no container
	$(MAKE) -C packaging deb

.PHONY: packages
packages:                   ## every release artifact: amd64 and arm64, deb and rpm
	$(MAKE) -C packaging packages

# --------------------------------------------------------------------------

# Everything CI runs, in the order it runs it, minus the toolchain installs.
# The point is that a red build can be reproduced with one command.
.PHONY: ci
ci: check-core test sanitize golden format-check test-daemon firmware  ## everything CI runs, except emulation

.PHONY: clean
clean:
	rm -rf build build-san build-O0 build-O3 .pio $(IMAGE_DIR)
	$(MAKE) -C packaging clean

.PHONY: help
help:
	@grep -E '^[a-z][a-z-]*:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
