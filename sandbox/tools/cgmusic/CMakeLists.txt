cmake_minimum_required(VERSION 3.18)
project(cgmusic VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(PkgConfig REQUIRED)
pkg_check_modules(FLUIDSYNTH REQUIRED IMPORTED_TARGET fluidsynth)
pkg_check_modules(EBUR128 REQUIRED IMPORTED_TARGET libebur128)
pkg_check_modules(SNDFILE REQUIRED IMPORTED_TARGET sndfile)
pkg_check_modules(MP3LAME REQUIRED IMPORTED_TARGET lame)
pkg_check_modules(PUGIXML REQUIRED IMPORTED_TARGET pugixml)
pkg_check_modules(LIBZIP REQUIRED IMPORTED_TARGET libzip)

add_library(cgmusic_core STATIC
  src/common.cpp
 src/balance.cpp
  src/midi.cpp
  src/program_map.cpp
  src/render.cpp
  src/loudness.cpp
  src/encode.cpp
  src/soundfont.cpp
 src/sends.cpp
  src/musicxml.cpp
)
target_include_directories(cgmusic_core PUBLIC src)
target_compile_options(cgmusic_core PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(cgmusic_core PUBLIC
  PkgConfig::FLUIDSYNTH PkgConfig::EBUR128 PkgConfig::SNDFILE PkgConfig::MP3LAME)
target_link_libraries(cgmusic_core PRIVATE PkgConfig::PUGIXML PkgConfig::LIBZIP)

add_executable(cgmusic src/main.cpp)
target_compile_options(cgmusic PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(cgmusic PRIVATE cgmusic_core)

enable_testing()
file(GLOB CGMUSIC_TEST_SOURCES CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_*.cpp)
add_executable(cgmusic_tests ${CGMUSIC_TEST_SOURCES})
target_include_directories(cgmusic_tests PRIVATE tests)
target_compile_options(cgmusic_tests PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(cgmusic_tests PRIVATE cgmusic_core)
add_test(NAME cgmusic_tests COMMAND cgmusic_tests)
