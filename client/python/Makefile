# Use these targets rather than raw uv lines: the generated stubs are committed,
# their imports are rewritten on the way out, and there is a check that holds
# both to proto/ — all of which a raw `protoc` line skips.

UV ?= uv
PROTO_ROOT := ../../proto
PROTO_OUT  := statemachined_client/_proto
PROTOS     := $(wildcard $(PROTO_ROOT)/statemachined/v1/*.proto) \
              $(wildcard $(PROTO_ROOT)/braemons/v1/*.proto)

.PHONY: help proto generate-into check-proto test test-unit lint fmt typecheck check build clean

help:
	@sed -n 's/^\([a-z-]*\):.*## \(.*\)/  \1|\2/p' $(MAKEFILE_LIST) | column -t -s '|'

# The generated code is committed (contracts/DAEMON_LAYOUT.md), so an install
# needs no protoc and grpcio-tools is a dev dependency rather than a runtime
# one. A reviewer sees an interface change as a diff.
proto: ## Regenerate statemachined_client/_proto/ from ../../proto
	@$(MAKE) --no-print-directory generate-into OUT=$(PROTO_OUT)
	@echo "$(PROTO_OUT)/"

# **The import rewrite is why this is a target and not a command.**
#
# protoc roots a generated module's imports at the proto path, so the stubs
# reach each other as `from statemachined.v1 import common_pb2` — and
# `statemachined` is the *daemon's* package name, which this client must not
# claim. vstimd's and mousewheeld's clients do not have this problem: their
# daemons are Rust, so the client owns the name. Here both are Python, and a
# client distribution that installed a top-level `statemachined` would
# overwrite the daemon's — on a rig box, where both are installed.
#
# So the generated imports are rewritten to be absolute within this package.
# It is one line of sed over three file kinds, and `check-proto` compares the
# rewritten output, so nothing can drift back.
generate-into:
	@mkdir -p $(OUT)
	@$(UV) run --group dev python -m grpc_tools.protoc \
	  --proto_path=$(PROTO_ROOT) \
	  --python_out=$(OUT) --pyi_out=$(OUT) --grpc_python_out=$(OUT) \
	  $(PROTOS)
	@find $(OUT) -name '*.py' -o -name '*.pyi' | xargs sed -i \
	  -e 's/^from statemachined\.v1 import /from statemachined_client._proto.statemachined.v1 import /' \
	  -e 's/^from braemons\.v1 import /from statemachined_client._proto.braemons.v1 import /'

# Against a fresh generation rather than against git: a *new* generated file is
# untracked, and `git diff` says nothing at all about an untracked file. A
# staleness check that goes quiet the first time a message is added is worse
# than no check.
check-proto: ## Fail if the committed stubs are not what proto/ produces
	@rm -rf build/proto-check
	@$(MAKE) --no-print-directory generate-into OUT=build/proto-check
	@# --exclude=__pycache__: a bytecode cache is not the interface.
	@diff -r --exclude=__pycache__ build/proto-check $(PROTO_OUT) || { \
	  echo "$(PROTO_OUT)/ is not what proto/ produces — the interface changed."; \
	  echo "run 'make proto' and commit the result with the change that caused it."; \
	  exit 1; \
	}
	@rm -rf build/proto-check

# The suite needs a daemon, and starts one itself: on a free port, in a
# temporary directory, against the simulated device. It skips rather than fails
# when the daemon is not installed, so a checkout that only has the client
# still runs the seam tests.
test: ## Everything: the seam with no daemon, and the client against one
	@$(UV) run --group dev pytest -q

test-unit: ## Only the seam — no daemon, no network
	@$(UV) run --group dev pytest -q tests/test_convert_seam.py

lint: ## ruff check + format --check
	@$(UV) run --group dev ruff check .
	@$(UV) run --group dev ruff format --check .

fmt: ## ruff format
	@$(UV) run --group dev ruff format .

typecheck: ## ty
	@$(UV) run --group dev ty check statemachined_client tests

check: check-proto lint typecheck test ## The proto, ruff, ty, and both suites

build: proto ## A wheel
	@$(UV) build

clean:
	@rm -rf dist build .pytest_cache .venv
