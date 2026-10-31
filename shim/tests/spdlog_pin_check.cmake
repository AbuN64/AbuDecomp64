# SPDX-License-Identifier: GPL-3.0-or-later
# One spdlog (hence one bundled fmt) for every target: the Android pin in fast3d.cmake equals the Windows pin in
# fast3d_win_deps.cmake. v1.14.1's fmt 10.2.1 fails to compile under NDK r29.
# Input: -DSHIM_DIR=<shim dir>.
function(spdlog_pin file)
    file(READ "${SHIM_DIR}/cmake/${file}" text)
    if(NOT text MATCHES "n64c_dep\\((win_)?spdlog https://github.com/gabime/spdlog.git (v[0-9.]+)")
        message(FATAL_ERROR "${file}: no pinned spdlog")
    endif()
    set(pin "${CMAKE_MATCH_2}" PARENT_SCOPE)
endfunction()
spdlog_pin(fast3d.cmake)
set(android_pin "${pin}")
spdlog_pin(fast3d_win_deps.cmake)
if(NOT android_pin STREQUAL pin)
    message(FATAL_ERROR "Android spdlog ${android_pin} differs from Windows spdlog ${pin}")
endif()
string(SUBSTRING "${android_pin}" 1 -1 android_version)
if(android_version VERSION_LESS 1.15.2)
    message(FATAL_ERROR "spdlog ${android_pin}: its bundled fmt does not compile under NDK r29")
endif()
