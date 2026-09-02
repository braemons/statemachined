add_library(doctest_main STATIC doctest_main.cpp)
target_include_directories(doctest_main PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/third_party)

# Mirrors firmware/core's layout, so a test lives next to the concern it tests.
set(FSMD_TESTS
  graph/state_graph
  random/rng
  machine/state_machine
  trial/trial_runner
  protocol/framing
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
