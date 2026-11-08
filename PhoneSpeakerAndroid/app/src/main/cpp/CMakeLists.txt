cmake_minimum_required(VERSION 3.22.1)
project(phonespeaker_native C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)

# Fetch and build libopus statically
set(OPUS_BUILD_PROGRAMS OFF CACHE BOOL "Disable opus programs" FORCE)
set(OPUS_BUILD_TESTING OFF CACHE BOOL "Disable opus testing" FORCE)
set(BUILD_TESTING OFF CACHE BOOL "Disable testing" FORCE)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "Build static libopus" FORCE)

FetchContent_Declare(
    libopus
    GIT_REPOSITORY https://github.com/xiph/opus.git
    GIT_TAG v1.5.2
    GIT_SHALLOW TRUE
)

FetchContent_MakeAvailable(libopus)

find_package(oboe REQUIRED CONFIG)

add_library(phonespeaker_native SHARED
    native_audio.cpp
    jitter_buffer.cpp
    opus_wrapper.cpp
    jni_bridge.cpp
)

target_include_directories(phonespeaker_native PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${libopus_SOURCE_DIR}/include
)

target_link_libraries(phonespeaker_native PRIVATE
    oboe::oboe
    opus
    log
    android
)

