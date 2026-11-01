#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Cross-build the AbuDecomp64 shim (n64compat + Fast3D) for Windows i686 (mingw-w64) from the job's worktree.
# Run from the repository root on a host with mingw-w64 (the rig); the build tree is a fresh temp directory
# (XBUILD_DIR to keep one) and the dependency sources are shared under N64C_DEPS_DIR.
set -eu
W=$(pwd)
B=${XBUILD_DIR:-$(mktemp -d)}
mkdir -p "$B/src"
cat > "$B/src/tc.cmake" <<'TC'
set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR i686)
set(CMAKE_C_COMPILER i686-w64-mingw32-gcc-posix)
set(CMAKE_CXX_COMPILER i686-w64-mingw32-g++-posix)
set(CMAKE_RC_COMPILER i686-w64-mingw32-windres)
set(CMAKE_FIND_ROOT_PATH /usr/i686-w64-mingw32)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_C_FLAGS_INIT "-static-libgcc")
set(CMAKE_CXX_FLAGS_INIT "-static-libgcc -static-libstdc++")
set(CMAKE_EXE_LINKER_FLAGS_INIT "-static -static-libgcc")
TC
cat > "$B/src/CMakeLists.txt" <<DR
cmake_minimum_required(VERSION 3.28)
project(xbuild C)
include(CTest)
include(FetchContent)
find_package(Python3 COMPONENTS Interpreter REQUIRED)
set(N64C_M32 ON)
set(N64C_DEPS_DIR ${N64C_DEPS_DIR:-$HOME/rig-state/deps} CACHE PATH "")
include($W/shim/cmake/deps.cmake)
set(SDL_SHARED OFF CACHE BOOL "" FORCE)
set(SDL_STATIC ON CACHE BOOL "" FORCE)
set(SDL_TEST_LIBRARY OFF CACHE BOOL "" FORCE)
set(SDL_TESTS OFF CACHE BOOL "" FORCE)
set(SDL_EXAMPLES OFF CACHE BOOL "" FORCE)
n64c_dep(SDL3 https://github.com/libsdl-org/SDL.git 7f3ae3d57459e59943a4ecfefc8f6277ec6bf540 GIT_SHALLOW FALSE)
set(CMAKE_DISABLE_PRECOMPILE_HEADERS ON)
FetchContent_MakeAvailable(SDL3)
unset(CMAKE_DISABLE_PRECOMPILE_HEADERS)
include($W/shim/cmake/n64compat_audio.cmake)
include($W/shim/cmake/n64compat.cmake)
include($W/shim/cmake/fast3d.cmake)
DR
cmake -S "$B/src" -B "$B/build" -G Ninja -DCMAKE_TOOLCHAIN_FILE="$B/src/tc.cmake" -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
# The generic shim tests link without the game's boot hooks (n64c_boot_missing, n64c_boot_run_ended), which are weak
# references on ELF and must be defined on PE; n64c_test_memory_win defines them, so it is the Windows executable here.
ninja -C "$B/build" -j "${JOBS:-8}" n64compat n64c_fast3d n64c_test_memory_win
echo "== loader test"
ctest --test-dir "$B/build" -R n64c_windows_gl_loader --no-tests=error --output-on-failure
echo "== BindFramebuffer references in the fast3d archive"
i686-w64-mingw32-nm --undefined-only "$B/build/libn64c_fast3d.a" | grep BindFramebuffer | sort -u
