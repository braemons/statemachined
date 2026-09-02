# Host build of the portable core, for unit tests in CI.
#
# The core is plain C++17 with no Arduino.h, so it compiles here exactly as it
# does on a board. That is the point: the native build is a real test of the
# firmware rather than a parallel implementation of it. Board builds go through
# PlatformIO; this file never sees a HAL.
cmake_minimum_required(VERSION 3.16)
project(fsmd_core LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_library(fsmd_core
  firmware/core/rng.cpp
  firmware/core/graph.cpp
  firmware/core/engine.cpp
)
target_include_directories(fsmd_core PUBLIC firmware/core)
target_compile_options(fsmd_core PRIVATE -Wall -Wextra -Wpedantic -Werror)

option(FSMD_SANITIZE "Build tests with ASan and UBSan" OFF)
if(FSMD_SANITIZE)
  add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer -g)
  add_link_options(-fsanitize=address,undefined)
endif()

enable_testing()
add_subdirectory(tests)
