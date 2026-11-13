cmake_minimum_required(VERSION 3.18)
project(cgsynth VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(PkgConfig REQUIRED)
pkg_check_modules(FLUIDSYNTH REQUIRED IMPORTED_TARGET fluidsynth)
pkg_check_modules(SQLITE3 REQUIRED IMPORTED_TARGET sqlite3)

add_library(cgmusic_core STATIC
  src/common.cpp
  src/midi.cpp
  src/program_map.cpp
  src/render.cpp
  src/soundfont.cpp
  src/wav.cpp
  src/muse/loader.cpp
  src/muse/catalog.cpp
  src/muse/mapping.cpp
  src/muse/render.cpp
)
target_include_directories(cgmusic_core PUBLIC src)
target_compile_options(cgmusic_core PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(cgmusic_core PUBLIC PkgConfig::FLUIDSYNTH PkgConfig::SQLITE3)
target_link_libraries(cgmusic_core PRIVATE ${CMAKE_DL_LIBS})

add_executable(cgsynth src/main.cpp)
target_compile_options(cgsynth PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(cgsynth PRIVATE cgmusic_core)

option(CGMUSIC_BUILD_PROBE "构建 muse_probe 探针工具" OFF)
if(CGMUSIC_BUILD_PROBE)
  add_executable(muse_probe tools/muse_probe.cpp)
  target_compile_options(muse_probe PRIVATE -Wall -Wextra -Wpedantic)
  target_link_libraries(muse_probe PRIVATE cgmusic_core)
endif()

enable_testing()
file(GLOB CGMUSIC_TEST_SOURCES CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_*.cpp)
add_executable(cgmusic_tests ${CGMUSIC_TEST_SOURCES})
target_include_directories(cgmusic_tests PRIVATE tests)
target_compile_options(cgmusic_tests PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(cgmusic_tests PRIVATE cgmusic_core)
add_test(NAME cgmusic_tests COMMAND cgmusic_tests)
