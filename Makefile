# The core is plain C++17 and builds on the host, so the fastest feedback loop
# in the repo needs no board attached. Use it before reaching for anything else.

BUILD ?= build

.PHONY: test
test:                       ## build and run the core unit tests
	cmake -S . -B $(BUILD) -DCMAKE_BUILD_TYPE=Debug
	cmake --build $(BUILD) -j
	ctest --test-dir $(BUILD) --output-on-failure

.PHONY: sanitize
sanitize:                   ## the same tests under ASan and UBSan
	cmake -S . -B build-san -DCMAKE_BUILD_TYPE=Debug -DFSMD_SANITIZE=ON
	cmake --build build-san -j
	ctest --test-dir build-san --output-on-failure

.PHONY: firmware
firmware:                   ## build for the reference board
	pio run -e uno_r4_minima

.PHONY: upload
upload:                     ## flash the reference board
	pio run -e uno_r4_minima -t upload

.PHONY: format
format:                     ## clang-format every source file in place
	find firmware tests -name '*.cpp' -o -name '*.h' \
	  | grep -v third_party | xargs clang-format -i

.PHONY: clean
clean:
	rm -rf build build-san build-O0 build-O3 .pio

.PHONY: help
help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
