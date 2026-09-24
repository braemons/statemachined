# SPDX-License-Identifier: GPL-3.0-or-later
add_library(doctest_main STATIC doctest_main.cpp)
target_include_directories(doctest_main PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/third_party)

# Mirrors firmware/core's layout, so a test lives next to the concern it tests.
set(STATEMACHINED_TESTS
  graph/graph_set
  random/rng
  io/input_conditioner
  io/reply_queue
  io/settings_store
  machine/state_machine
  machine/global_timers
  trial/trial_runner
  protocol/cobs
  protocol/framing
  protocol/graph_builder
  protocol/host_link_session
)

# The tests' own half of the link: commands written as the JSON the NDJSON
# wire carried, and replies rendered back into it. See support/link_json.h.
add_library(link_test_support STATIC support/link_json.cpp support/wire_json.cpp)
target_link_libraries(link_test_support PUBLIC statemachined_core)
target_include_directories(link_test_support PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/support)

foreach(path IN LISTS STATEMACHINED_TESTS)
  get_filename_component(t ${path} NAME)
  get_filename_component(dir ${path} DIRECTORY)
  add_executable(test_${t} core/${dir}/test_${t}.cpp)
  target_link_libraries(test_${t} PRIVATE statemachined_core link_test_support doctest_main)
  target_include_directories(test_${t} PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/third_party
    ${CMAKE_CURRENT_SOURCE_DIR}/core)  # helpers.h, shared across the groups
  add_test(NAME ${t} COMMAND test_${t})
endforeach()

# Links against statemachined_engine ONLY -- no protocol object is on this command line.
# It is a link-time proof that a graph can be built in C++ and run with no wire
# anywhere near it, which is what makes the native build a real test of the
# firmware and what a Linux or bench-rig embedding would depend on.
add_executable(test_engine_standalone core/machine/test_engine_standalone.cpp)
target_link_libraries(test_engine_standalone PRIVATE statemachined_engine doctest_main)
target_include_directories(test_engine_standalone PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party
  ${CMAKE_CURRENT_SOURCE_DIR}/core)
add_test(NAME engine_standalone COMMAND test_engine_standalone)
