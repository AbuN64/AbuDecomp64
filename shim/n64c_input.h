/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Hand-written host input adapter. SDL is sampled by the window owner; guest SI
 * reads only the last complete snapshot, never pumps a window from a guest thread.
 * Four N64 ports: port N reads the Nth gamepad that was plugged in (hot-plug aware) plus whichever keyboard and
 * mouse binds that player holds (n64c_binds.h). */
#ifndef N64C_INPUT_H
#define N64C_INPUT_H
#include "n64c_binds.h"
#include "n64c_cont.h"
#include <SDL3/SDL.h>
#include <stdbool.h>
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
enum {
    N64C_PAD_A = 0x8000, N64C_PAD_B = 0x4000, N64C_PAD_Z = 0x2000,
    N64C_PAD_START = 0x1000, N64C_PAD_UP = 0x0800, N64C_PAD_DOWN = 0x0400,
    N64C_PAD_LEFT = 0x0200, N64C_PAD_RIGHT = 0x0100,
    N64C_PAD_L = 0x0020, N64C_PAD_R = 0x0010,
    N64C_PAD_CUP = 0x0008, N64C_PAD_CDOWN = 0x0004,
    N64C_PAD_CLEFT = 0x0002, N64C_PAD_CRIGHT = 0x0001
};
/* Bit of the SI button word for an action; 0 for the four stick directions. */
unsigned n64c_action_button(int action);
/* The live bind table, valid before start and after stop. Edit it with n64c_binds_set(), then call
 * n64c_input_binds_commit() to keep the change. Only the window thread may touch it. */
N64cBinds *n64c_input_binds(void);
/* The controls seam. The host program binds a context with two callbacks and owns the file and its lifecycle: the shim
 * reads and writes no controls file (the host archives the table in its one config file, with the text form of
 * n64c_binds_format / n64c_binds_parse). Both callbacks run on the window thread, with no shim lock held.
 *   load:    at every input start, with a copy of the table in force (the factory table until something else was kept);
 *            fills it from the host's archive and returns true, or returns false when it has none (the table stays).
 *   request: when the Controls page asks to keep an edit, with the complete table requested. The host validates it,
 *            applies it to its own state and saves its archive (atomically), and returns true only when all of that held.
 *            On false it has changed nothing and writes the reason into error (capacity bytes, supplied by the shim); the
 *            shim then puts the table back to the last one the host accepted and reports the reason.
 * The context is borrowed: the host keeps it alive from bind until n64c_input_host_unbind. The registration is the host's
 * and outlives n64c_game_main and every n64c_input_start/stop: the host binds before calling it, the game's boot binds its
 * flags-only host only when none is bound, and only n64c_input_host_unbind removes a binding. With none bound the Controls
 * page refuses every change. There is no file fallback. */
typedef bool (*N64cInputLoadFn)(void *context, N64cBinds *binds);
typedef bool (*N64cInputRequestFn)(void *context, const N64cBinds *requested, char *error, size_t capacity);
/* False, with nothing changed, when a callback is missing or a host is already bound. */
bool n64c_input_host_bind(void *context, N64cInputLoadFn load, N64cInputRequestFn request);
void n64c_input_host_unbind(void);
/* Keeps the edit made to the live table: asks the host. True: the host accepted and saved it. False: the table is back to
 * the last accepted one and error says why. */
bool n64c_input_binds_commit(char *error, size_t capacity);
/* Factory bindings applied to the live table (all players or one), then committed as above. */
bool n64c_input_binds_reset(int player /* -1 = all */, char *error, size_t capacity);
/* Touch zones (n64c_touch.h): edit mode on/off. The layout lives for the run; no file keeps it. */
void n64c_input_touch_edit(int editing);
/* NULL/offscreen windows and every N64C_LOCKSTEP environment disable live input. */
int n64c_input_start(SDL_Window *window);
void n64c_input_event(const SDL_Event *event);
void n64c_input_update(void);
void n64c_input_stop(void);
int n64c_input_axis(int raw, int deadzone);
/* Gamepad owning an N64 port (NULL name = none) and the port's presence as the guest sees it. */
const char *n64c_input_pad_name(int port);
int n64c_input_port_present(int port);
/* Rebinding: wait for the next input of one device (N64C_DEV_*). Escape cancels. While waiting, and while the
 * Controls page is open, the game sees neutral pads. poll returns 0 waiting, 1 captured (*out set), -1 cancelled
 * or idle. */
int n64c_input_capture_begin(int device);
int n64c_input_capture_poll(N64cBind *out);
void n64c_input_capture_cancel(void);
int n64c_input_capturing(void);
/* Short label of a bind for the Controls page ("X", "LB", "LS RIGHT", "MOUSE 1", "LOOK LEFT"). */
void n64c_bind_label(const N64cBind *bind, char *out, size_t cap);
#ifdef __cplusplus
}
#endif
#endif
