add_library(doctest_main STATIC doctest_main.cpp)
target_include_directories(doctest_main PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/third_party)

foreach(t rng state_graph state_machine trial_runner)
  add_executable(test_${t} core/test_${t}.cpp)
  target_link_libraries(test_${t} PRIVATE fsmd_core doctest_main)
  target_include_directories(test_${t} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/third_party)
  add_test(NAME ${t} COMMAND test_${t})
endforeach()
