# SPDX-License-Identifier: GPL-3.0-or-later
# Host build of the portable core, for unit tests in CI.
#
# The core is plain C++17 with no Arduino.h, so it compiles here exactly as it
# does on a board. That is the point: the native build is a real test of the
# firmware rather than a parallel implementation of it. Board builds go through
# PlatformIO; this file never sees a HAL.
cmake_minimum_required(VERSION 3.16)
project(statemachined_core LANGUAGES CXX)

# Always, not only when a VS Code extension happens to pass it. clangd reads
# this file to learn the include paths, and since the core's headers are
# included path-qualified ("graph/state.h") there is nothing for it to guess
# from if the database is missing -- the editor simply reports every include as
# unresolved. See .clangd.
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Two libraries, not one, and the split is the layering made real.
#
# statemachined_engine is the part that runs a trial: a graph, the timings, the scan
# loop. It has no idea a wire exists. statemachined_core adds the protocol on top.
#
# Splitting them means the dependency cannot silently reverse. tests/ links one
# binary against statemachined_engine alone (see tests/CMakeLists.txt), so if the state
# machine ever grows a dependency on JSON or framing, that link fails -- which
# is a better answer than a comment asking people not to.
add_library(statemachined_engine
  firmware/core/graph/graph_set.cpp
  firmware/core/random/rng.cpp
  firmware/core/random/random_distribution.cpp
  firmware/core/io/input_conditioner.cpp
  firmware/core/machine/state_machine.cpp
  firmware/core/machine/global_timers.cpp
  firmware/core/trial/trial_runner.cpp
)
target_include_directories(statemachined_engine PUBLIC firmware/core)
target_compile_options(statemachined_engine PRIVATE -Wall -Wextra -Wpedantic -Werror)

add_library(statemachined_core
  firmware/core/io/reply_queue.cpp
  firmware/core/io/settings_store.cpp
  firmware/core/protocol/crc16.cpp
  firmware/core/protocol/framing.cpp
  firmware/core/protocol/graph_builder.cpp
  firmware/core/protocol/host_link_session.cpp
  firmware/core/protocol/json.cpp
  firmware/core/protocol/msg_type.cpp
)
target_link_libraries(statemachined_core PUBLIC statemachined_engine)
target_compile_options(statemachined_core PRIVATE -Wall -Wextra -Wpedantic -Werror)

option(STATEMACHINED_SANITIZE "Build tests with ASan and UBSan" OFF)
if(STATEMACHINED_SANITIZE)
  add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer -g)
  add_link_options(-fsanitize=address,undefined)
endif()

enable_testing()
add_subdirectory(tests)

# The host HAL, built here so that hal.h is compiled by something CI runs. It is
# not linked into the tests: those drive StateMachine and HostLinkSession
# directly, which is the seam that matters. This target exists to keep the
# interface honest -- a board-only header rots between board builds.
add_library(statemachined_hal_native firmware/hal/native.cpp)
target_link_libraries(statemachined_hal_native PUBLIC statemachined_engine)
target_compile_options(statemachined_hal_native PRIVATE -Wall -Wextra -Wpedantic -Werror)

# The firmware, on this machine, speaking the real protocol down a pipe. It is
# what the daemon's integration tests talk to, so that they exercise the
# device's own refusals and the device's own result chunker rather than a mock
# built from somebody's reading of the protocol document.
add_executable(statemachined_native_device firmware/native/statemachined_native_device.cpp)
target_link_libraries(statemachined_native_device
  PRIVATE statemachined_core statemachined_hal_native)
target_compile_options(statemachined_native_device
  PRIVATE -Wall -Wextra -Wpedantic -Werror)
