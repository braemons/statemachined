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
	cmake -S . -B build-san -DCMAKE_BUILD_TYPE=Debug -DFSMD_SANITIZE=ON
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
firmware:                   ## build for the reference board (demo mode on)
	pio run -e $(BOARD)

# Demo mode is 4.6 KB of SRAM on a 32 KB part, so a rig build drops it. Built
# here as well as by CI, or the #else half of that switch rots unnoticed.
.PHONY: firmware-rig
firmware-rig:               ## the same, with demo mode compiled out
	PLATFORMIO_BUILD_FLAGS=-DFSMD_DEMO=0 pio run -e $(BOARD)

# Emulation. Covers what the host build cannot compile -- the pin map, the port
# registers, the timer ISR, the protocol over a real UART -- and says nothing
# about timing. See emulation/README.md.
RENODE_VERSION ?= 1.16.1

.PHONY: emulate
emulate:                    ## run the firmware under Renode, in Robot tests
	pio run -e $(BOARD)_sci
	renode-test emulation/tests/fsmd.robot

.PHONY: upload
upload:                     ## flash the reference board
	pio run -e uno_r4_minima -t upload

# Pinned to match .github/workflows/ci.yml. clang-format's output changes
# between major versions, and `BasedOnStyle: Google` in .clang-format resolves
# against whichever version is running, so an unpinned one reformats files CI
# then rejects. Install it from pip, not from a package manager.
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

.PHONY: install-pio
install-pio:                ## PlatformIO, for the board and emulation builds
	pip install platformio

.PHONY: install-clang-format
install-clang-format:       ## the pinned clang-format, from pip not the distro
	pip install clang-format==$(CLANG_FORMAT_PIN)

# The Robot keyword library ships inside Renode, so a different Renode is a
# different set of keywords. Pinned to the devcontainer's version.
RENODE_URL = https://github.com/renode/renode/releases/download/v$(RENODE_VERSION)/renode-$(RENODE_VERSION).linux-portable-dotnet.tar.gz

.PHONY: install-renode
install-renode:             ## the pinned Renode, portable, into /opt/renode
	curl -fsSL $(RENODE_URL) -o /tmp/renode.tar.gz
	mkdir -p /opt/renode
	sudo tar xzf /tmp/renode.tar.gz -C /opt/renode --strip-components=1
	sudo ln -sf /opt/renode/renode-test /usr/local/bin/renode-test
	pip install -r /opt/renode/tests/requirements.txt

# --------------------------------------------------------------------------

# Everything CI runs, in the order it runs it, minus the toolchain installs.
# The point is that a red build can be reproduced with one command.
.PHONY: ci
ci: check-core test sanitize golden format-check firmware firmware-rig  ## everything CI runs, except emulation

.PHONY: clean
clean:
	rm -rf build build-san build-O0 build-O3 .pio

.PHONY: help
help:
	@grep -E '^[a-z][a-z-]*:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
