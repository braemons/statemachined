# Host build of the portable core, for unit tests in CI.
#
# The core is plain C++17 with no Arduino.h, so it compiles here exactly as it
# does on a board. That is the point: the native build is a real test of the
# firmware rather than a parallel implementation of it. Board builds go through
# PlatformIO; this file never sees a HAL.
cmake_minimum_required(VERSION 3.16)
project(fsmd_core LANGUAGES CXX)

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
# fsmd_engine is the part that runs a trial: a graph, the timings, the scan
# loop. It has no idea a wire exists. fsmd_core adds the protocol on top.
#
# Splitting them means the dependency cannot silently reverse. tests/ links one
# binary against fsmd_engine alone (see tests/CMakeLists.txt), so if the state
# machine ever grows a dependency on JSON or framing, that link fails -- which
# is a better answer than a comment asking people not to.
add_library(fsmd_engine
  firmware/core/graph/state_graph.cpp
  firmware/core/random/rng.cpp
  firmware/core/random/random_distribution.cpp
  firmware/core/machine/state_machine.cpp
  firmware/core/trial/trial_runner.cpp
)
target_include_directories(fsmd_engine PUBLIC firmware/core)
target_compile_options(fsmd_engine PRIVATE -Wall -Wextra -Wpedantic -Werror)

add_library(fsmd_core
  firmware/core/protocol/crc16.cpp
  firmware/core/protocol/framing.cpp
  firmware/core/protocol/graph_builder.cpp
  firmware/core/protocol/host_link_session.cpp
  firmware/core/protocol/json.cpp
)
target_link_libraries(fsmd_core PUBLIC fsmd_engine)
target_compile_options(fsmd_core PRIVATE -Wall -Wextra -Wpedantic -Werror)

option(FSMD_SANITIZE "Build tests with ASan and UBSan" OFF)
if(FSMD_SANITIZE)
  add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer -g)
  add_link_options(-fsanitize=address,undefined)
endif()

enable_testing()
add_subdirectory(tests)
