# Builds Firely, the on-phone AI (llama.cpp + our small wrapper) into one self-contained library.
# .github/scripts/build-brain.sh builds it for each phone type and puts it in jniLibs;
# llama.cpp's source is placed in ./llama.cpp by .github/scripts/prepare-android.sh.
cmake_minimum_required(VERSION 3.22)
project(brain CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)  # static llama.cpp goes inside a shared library
set(LLAMA_SRC ${CMAKE_CURRENT_SOURCE_DIR}/llama.cpp CACHE PATH "llama.cpp source")
set(BRAIN_NAME brain CACHE STRING "library name (libNAME.so)")

set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_COMMON OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER OFF CACHE BOOL "" FORCE)
set(LLAMA_CURL OFF CACHE BOOL "" FORCE)
set(LLAMA_OPENSSL OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_MTMD ON CACHE BOOL "" FORCE)  # pictures (the model's eyes)
set(GGML_OPENMP OFF CACHE BOOL "" FORCE)
set(GGML_NATIVE OFF CACHE BOOL "" FORCE)
set(GGML_LLAMAFILE ON CACHE BOOL "" FORCE)

if(ANDROID AND ANDROID_ABI STREQUAL "x86_64")
    # Emulators and Chromebooks: modern x86 CPUs all have these.
    set(GGML_AVX ON CACHE BOOL "" FORCE)
    set(GGML_AVX2 ON CACHE BOOL "" FORCE)
    set(GGML_FMA ON CACHE BOOL "" FORCE)
    set(GGML_F16C ON CACHE BOOL "" FORCE)
endif()

add_subdirectory(${LLAMA_SRC} llama.cpp EXCLUDE_FROM_ALL)

add_library(brain SHARED brain_core.cpp $<$<BOOL:${ANDROID}>:brain_jni.cpp>)
target_link_libraries(brain PRIVATE mtmd llama ggml)
if(ANDROID)
    target_link_libraries(brain PRIVATE log android)
    # Smaller library, faster loading.
    target_link_options(brain PRIVATE -Wl,--gc-sections -Wl,-z,max-page-size=16384)
endif()
target_compile_options(brain PRIVATE -O3)
set_target_properties(brain PROPERTIES OUTPUT_NAME ${BRAIN_NAME})
