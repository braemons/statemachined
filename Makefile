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
	cmake -S . -B $(BUILD) -DCMAKE_BUILD_TYPE=Debug \
	  -DSTATEMACHINED_FIRMWARE_VERSION=$(STATEMACHINED_FIRMWARE_VERSION)
	cmake --build $(BUILD) -j
	ctest --test-dir $(BUILD) --output-on-failure

.PHONY: check-core
check-core:                 ## enforce the portable core's constraints
	@./tools/check-core-purity.sh

.PHONY: check-proto
# **protoc** catches a `.proto` that does not parse: `proto/statemachined/v1/` is
# this daemon's interface -- types *and* rpcs, hand-authored
# (`contracts/DAEMON_LAYOUT.md`) -- and `proto/statemachined/link/v1/` is the
# board's. The generated code for each is committed and compared against a fresh
# generation: the board's here, the daemon's in `rust-check-proto`, the Python
# client's in `make client`.
#
# The `.tdr` taxonomy, `proto/braemons/v1/`, is vendored byte-identically from
# `contracts/vendored/proto/` because neither this daemon nor triald owns it.
# `daemon-rs/tests/outcomes.rs` holds the firmware enum and the graph editor's
# menu to it.

# **The browser's protobuf client is generated and committed**, like the
# daemon's wire types and for the same reason: a checkout builds without the
# generator. The daemon embeds `client/web/`, so a bundle produced at build time
# would make npm a build dependency of every release. `npm ci` installs exactly what package-lock.json pins, so the bundle
# is reproducible; `check-web` is what holds it to the proto.
.PHONY: web check-web
web:                        ## regenerate client/web/elements/daemon_api_client.js from proto/
	@cd client/web && npm ci --silent --no-audit --no-fund && node build_daemon_api_client.mjs

check-web:                  ## fail if the committed browser client is not what proto/ produces
	@mkdir -p build
	@cp client/web/elements/daemon_api_client.js build/web-check.js 2>/dev/null || true
	@$(MAKE) --no-print-directory web
	@diff -q build/web-check.js client/web/elements/daemon_api_client.js >/dev/null || { \
	  echo "client/web/elements/daemon_api_client.js is not what proto/ produces:"; \
	  diff build/web-check.js client/web/elements/daemon_api_client.js | head -20; \
	  echo "it has been regenerated — commit it with the change that caused it."; \
	  exit 1; \
	}
	@rm -f build/web-check.js

# The Python client is its own project with its own Makefile, and is not part
# of `ci` for the same reason `check-web` is not: it needs uv to build a second
# environment, and a network the first time. Its own `check` regenerates its
# stubs from proto/, lints, typechecks, and runs both suites — the second of
# which stands a real daemon up and talks to it over gRPC.
.PHONY: client
client:                     ## the Python client's checks: its stubs, ruff, ty, both suites
	@$(MAKE) --no-print-directory -C client/python check

check-proto:                ## the protos compile, and the board's generated code is current
	@protoc --proto_path=proto --descriptor_set_out=/dev/null \
	  proto/statemachined/v1/*.proto proto/braemons/v1/*.proto \
	  proto/statemachined/link/v1/*.proto
	@# The board's half of the link, like the daemon's, is compared against a
	@# fresh generation: a link.proto edited without `make firmware-proto` is a
	@# board built against last week's wire.
	@rm -rf build/firmware-proto-check
	@$(MAKE) --no-print-directory firmware-proto FIRMWARE_PROTO_OUT=build/firmware-proto-check
	@diff -r build/firmware-proto-check/statemachined firmware/core/proto/statemachined >/dev/null || { \
	  echo "firmware/core/proto/ is not what proto/statemachined/link/ produces."; \
	  echo "run 'make firmware-proto' and commit the result with the change that caused it."; \
	  exit 1; \
	}
	@rm -rf build/firmware-proto-check
	@echo "  proto: $$(grep -c '^  rpc ' proto/statemachined/v1/service.proto) rpcs in $$(grep -c '^service ' proto/statemachined/v1/service.proto) services"

# The link proto is generated for the board with nanopb and committed, like the
# daemon's stubs: a board build needs no protoc and a wire change arrives as a
# diff. The runtime it links is vendored at the same version in
# firmware/third_party/nanopb, and the two must move together.
FIRMWARE_PROTO_OUT ?= firmware/core/proto
.PHONY: firmware-proto
firmware-proto:             ## regenerate firmware/core/proto/ from proto/statemachined/link/
	@mkdir -p $(FIRMWARE_PROTO_OUT)
	@uvx --from nanopb==0.4.9.1 nanopb_generator -I proto -D $(FIRMWARE_PROTO_OUT) \
	  -f firmware/core/proto/link.options proto/statemachined/link/v1/link.proto

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

# The version the firmware reports in hello_ack, from the git tag like every
# other artifact (packaging/scripts/git-version.sh, which also honours
# STATEMACHINED_VERSION). PlatformIO reads it from the environment. A checkout
# with no reachable tag builds the 0.0.0 sentinel; `image`, which is what gets
# flashed onto rigs, refuses to.
ifndef STATEMACHINED_FIRMWARE_VERSION
STATEMACHINED_FIRMWARE_VERSION := $(shell packaging/scripts/git-version.sh 2>/dev/null || echo 0.0.0)
endif
export STATEMACHINED_FIRMWARE_VERSION

.PHONY: firmware
firmware:                   ## build for the reference board
	pio run -e $(BOARD)

# Emulation: emulation/ holds the Renode platform the reference board runs
# under, for poking at the HAL by hand. It has no test suite: the Robot suite
# that was here spoke the board's NDJSON wire from Python, and went with both.
# See emulation/README.md.
RENODE_VERSION ?= 1.16.1

.PHONY: upload
upload:                     ## flash the reference board
	pio run -e $(BOARD) -t upload

# Talking to a board from a terminal is the client's job, through the daemon:
# `statemachinectl`, from client/python. `make bench` runs a daemon in front of
# TARGET, and this asks it things. TARGET is a device path, a host:port, or a
# socket:// URL -- the same daemon reaches a board on a network as on a cable.
TARGET ?= /dev/ttyACM0

.PHONY: bringup
bringup:                    ## ask the bench daemon something: make bringup ARGS="device"
	uv run --project client/python statemachinectl --rig $(BENCH_HOST):$(BENCH_PORT) $(ARGS)

# The firmware's own session and engine, built for this machine. Named by the
# integration tests' skip message and by the bench, both of which are useless
# without it, and it is a fraction of `make test`: one binary, no ctest.
.PHONY: integration-device
integration-device:         ## build build/statemachined_native_device on its own
	cmake -S . -B $(BUILD) -DCMAKE_BUILD_TYPE=Debug \
	  -DSTATEMACHINED_FIRMWARE_VERSION=$(STATEMACHINED_FIRMWARE_VERSION)
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
BENCH_CONFIG    ?= tools/bench/statemachined_bench_rig_config.toml
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
	cargo run --quiet --manifest-path daemon-rs/Cargo.toml -- serve -t $(TARGET) \
	  --config $(BENCH_CONFIG) \
	  --host $(BENCH_HOST) --port $(BENCH_PORT) $(ARGS)

# The other half of the no-board path: the firmware's own session and engine,
# built for this machine, on a TCP port the daemon can dial. Not a mock -- see
# daemon-rs/src/native_device_on_a_socket.rs.
#
# `statemachined device` because the bridge ships: an operator who has
# installed the package and has no board runs the identical command.
.PHONY: bench-device
bench-device: integration-device  ## the native device on socket://127.0.0.1:5300
	STATEMACHINED_NATIVE_DEVICE=$(abspath $(BUILD))/statemachined_native_device \
	  cargo run --quiet --manifest-path daemon-rs/Cargo.toml -- device $(ARGS)

# The trial loop across the daemons is **not** here. It lives in the contracts
# repo (`e2e-tests/`), with the other tests that are about more than one
# daemon; `make e2e` below runs it against this checkout.

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
	@if [ "$(STATEMACHINED_FIRMWARE_VERSION)" = 0.0.0 ]; then \
	  echo "make image: no version. Run packaging/scripts/git-version.sh to see why," >&2; \
	  echo "  or pass STATEMACHINED_VERSION=<version>." >&2; exit 1; fi
	rm -rf $(IMAGE_DIR)
	mkdir -p $(IMAGE_DIR)
	$(MAKE) firmware
	cp .pio/build/$(BOARD)/firmware.bin $(IMAGE_DIR)/statemachined-$(BOARD).bin
	cp .pio/build/$(BOARD)/firmware.elf $(IMAGE_DIR)/statemachined-$(BOARD).elf
	@{ \
	  echo "statemachined firmware for the $(BOARD)"; \
	  echo; \
	  echo "version: $(STATEMACHINED_FIRMWARE_VERSION)"; \
	  echo "commit: $(IMAGE_SHA)"; \
	  echo "built:  $$(date -u +%Y-%m-%dT%H:%M:%SZ)"; \
	  echo "pio:    $$(pio --version)"; \
	  echo; \
	  echo "statemachined-$(BOARD).bin"; \
	  echo "  Holds no graph until one is uploaded, and comes back up running"; \
	  echo "  whatever it was last saved with. Wiring in docs/operations/hardware.md"; \
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
# see packaging/README.md and docs/developer/daemon.md §6 -- and these lines exist so that
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
# Documentation (MkDocs + Material, via uv; see docs/pyproject.toml)
# --------------------------------------------------------------------------

# Live preview at http://127.0.0.1:8000 with auto-reload.
.PHONY: docs
docs:                       ## live docs at http://127.0.0.1:8000
	uv run --project docs mkdocs serve

# Static site build to site/ (matches the Read the Docs build).
.PHONY: docs-build
docs-build:                 ## build the static docs site to site/
	uv run --project docs mkdocs build --strict

# --------------------------------------------------------------------------

# Everything CI runs, in the order it runs it, minus the toolchain installs.
# The point is that a red build can be reproduced with one command.
.PHONY: ci
ci: check-core check-proto test sanitize golden format-check rust-check client firmware  ## everything CI runs

# -- the daemon -------------------------------------------------------------

.PHONY: rust
rust:  ## build the daemon
	cargo build --manifest-path daemon-rs/Cargo.toml

.PHONY: rust-proto
rust-proto:  ## regenerate daemon-rs/src/wire/ from proto/
	cargo run --quiet --manifest-path tools/protogen/Cargo.toml

.PHONY: rust-check-proto
rust-check-proto:  ## fail if the committed wire types are not what proto/ produces
	@rm -rf target/proto-check
	@cargo run --quiet --manifest-path tools/protogen/Cargo.toml -- target/proto-check
	@diff -r --exclude=mod.rs target/proto-check daemon-rs/src/wire || { \
	  echo "daemon-rs/src/wire/ is not what proto/ produces: run 'make rust-proto'"; \
	  exit 1; \
	}

# Needs the native device: the upload test commits every compiled set to the
# firmware built for this machine, and skips, saying so, without it.
.PHONY: rust-test
rust-test: integration-device  ## the daemon's tests
	cargo test --manifest-path daemon-rs/Cargo.toml

.PHONY: rust-check
rust-check: rust rust-check-proto rust-test  ## everything the daemon checks
	cargo clippy --manifest-path daemon-rs/Cargo.toml --all-targets -- -D warnings

# The family's e2e suite -- contracts/e2e-tests -- against this checkout: the
# daemon cargo builds, first on PATH, and the native device from build/. The
# suite's environment gets triald's daemon and the two clients from the
# checkouts beside this one; statemachined has no Python to install.
E2E     ?= ../contracts/e2e-tests
TRIALD  ?= ../triald
VSTIMD  ?= ../vstimd

.PHONY: e2e
e2e: rust integration-device  ## the family's e2e suite, against this checkout
	cd $(E2E) && uv sync --quiet --group dev
	@printf 'statemachined-client @ file://%s\n' "$(abspath client/python)" > $(E2E)/.local-overrides.txt
	cd $(E2E) && uv pip install --quiet --overrides .local-overrides.txt \
	  -e "$(abspath $(TRIALD))/daemon[serve]" \
	  -e $(abspath client/python) \
	  -e $(abspath $(VSTIMD))/client/python
	cd $(E2E) && \
	  VSTIMD_BINARY=$(abspath $(VSTIMD))/target/release/vstimd \
	  STATEMACHINED_NATIVE_DEVICE=$(abspath $(BUILD))/statemachined_native_device \
	  PATH=$(abspath target/debug):$$(pwd)/.venv/bin:$$PATH \
	  .venv/bin/python -m pytest -v $(ARGS)

.PHONY: clean
clean:
	rm -rf build build-san build-O0 build-O3 .pio $(IMAGE_DIR)
	$(MAKE) -C packaging clean

.PHONY: help
help:
	@grep -E '^[a-z][a-z0-9-]*:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
