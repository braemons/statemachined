add_library(doctest_main STATIC doctest_main.cpp)
target_include_directories(doctest_main PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/third_party)

# Mirrors firmware/core's layout, so a test lives next to the concern it tests.
set(FSMD_TESTS
  graph/state_graph
  random/rng
  machine/state_machine
  trial/trial_runner
  protocol/framing
  protocol/graph_builder
  protocol/host_link_session
  protocol/json
)

foreach(path IN LISTS FSMD_TESTS)
  get_filename_component(t ${path} NAME)
  get_filename_component(dir ${path} DIRECTORY)
  add_executable(test_${t} core/${dir}/test_${t}.cpp)
  target_link_libraries(test_${t} PRIVATE fsmd_core doctest_main)
  target_include_directories(test_${t} PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/third_party
    ${CMAKE_CURRENT_SOURCE_DIR}/core)  # helpers.h, shared across the groups
  add_test(NAME ${t} COMMAND test_${t})
endforeach()

# Links against fsmd_engine ONLY -- no protocol object is on this command line.
# It is a link-time proof that a graph can be built in C++ and run with no wire
# anywhere near it, which is what makes the native build a real test of the
# firmware and what a Linux or bench-rig embedding would depend on.
add_executable(test_engine_standalone core/machine/test_engine_standalone.cpp)
target_link_libraries(test_engine_standalone PRIVATE fsmd_engine doctest_main)
target_include_directories(test_engine_standalone PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party
  ${CMAKE_CURRENT_SOURCE_DIR}/core)
add_test(NAME engine_standalone COMMAND test_engine_standalone)
