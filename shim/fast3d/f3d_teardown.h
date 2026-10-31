// SPDX-License-Identifier: GPL-3.0-or-later
// The order a Fast3D window is taken down in. Mesa's D3D12 GL driver (Windows) keeps a D3D12 device and its WARP worker
// threads behind the context; if the process ends first, the driver's DLLs detach inside ExitProcess while those
// threads still call into freed objects and the run dies with an access violation. So the context leaves the thread and
// is destroyed while the window still exists, the window goes next, and the video subsystem, which unloads the GL
// driver, goes last, here on the owning thread.
#pragma once
#include <SDL3/SDL.h>
#include <cstdio>
#include <cstdlib>

namespace N64c {
// The four SDL calls the order is made of, as a table so the unit drives the order with recording ops and the window
// passes the real ones.
struct F3dDisplayOps {
    bool (*release_context)(SDL_Window*);
    void (*destroy_context)(SDL_GLContext);
    void (*destroy_window)(SDL_Window*);
    void (*quit_video)();
};
inline const F3dDisplayOps& F3dSdlDisplayOps()
{
    static const F3dDisplayOps ops = {
        [](SDL_Window* window) { return SDL_GL_MakeCurrent(window, nullptr); },
        [](SDL_GLContext context) { SDL_GL_DestroyContext(context); },
        [](SDL_Window* window) { SDL_DestroyWindow(window); },
        []() { SDL_QuitSubSystem(SDL_INIT_VIDEO); },
    };
    return ops;
}
inline void F3dReleaseDisplay(const F3dDisplayOps& ops, SDL_Window* window, SDL_GLContext context, bool unloadDriver)
{
    if (context) {
        if (!ops.release_context(window)) {
            std::fprintf(stderr, "f3d teardown: release the context: %s\n", SDL_GetError());
            std::_Exit(78);
        }
        ops.destroy_context(context);
    }
    if (window)
        ops.destroy_window(window);
    if (unloadDriver)
        ops.quit_video();
}
inline void F3dReleaseDisplay(SDL_Window* window, SDL_GLContext context, bool unloadDriver)
{
    F3dReleaseDisplay(F3dSdlDisplayOps(), window, context, unloadDriver);
}
} // namespace N64c
