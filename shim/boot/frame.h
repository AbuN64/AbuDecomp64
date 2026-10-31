/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_BOOT_FRAME_H
#define N64C_BOOT_FRAME_H
#include "n64c_os.h"
#include "n64c_menu.h"
#include <stdbool.h>
#include <stdint.h>
/* audio_abi: N64aFlavour of the game's audio microcode, or -1 to refuse audio tasks. */
/* window_width/height 0: offscreen software rendering (capture); otherwise a visible window of that size. */
/* Program and user files for the renderer (shader archive, settings file; NULL: beside the executable) and an
 * offscreen live run (no visible window). Applies to the next n64c_boot_frame_start. */
void n64c_boot_frame_host(const char *shaders,const char *settings,int offscreen);
/* Frame-rate unlock (--fps-unlock, N64C_FPS_UNLOCK=1): between two game frames the live loop draws the scene again with
 * its matrices moved part of the way (n64c_f3d_interp_draw) and presents that, as fast as the cap allows, instead of
 * showing the VI's picture once per retrace. Presentation only: the guest's clock, the VI, audio, input polls and
 * the TAS recorder run exactly as without it, so a movie replays to the same per-poll state. A run with N64C_SPEED=0
 * (no wall-clock wait) has no time to draw in and is unchanged. The policy (unlock, cap, vsync) lives here; the host
 * wires it through n64c_presentation_apply and saves it. */
/* The rate the unlock presents at when the cap is AUTO (text NULL, empty or "1"): the display's refresh rate or 120,
 * whichever is more (a window on a faster screen gets its rate; an offscreen run, which has no display, 120); "max" no
 * limit; else a rate 2..100000. n64c_unlock_rate returns 0 for text that is none of these. */
int n64c_unlock_rate(const char *text,u32 refresh_hz,u32 *hz);
/* The cap text of N64C_FPS_UNLOCK / a config: "", "1" or "auto" -> AUTO, "max" -> MAX, 2..100000 -> FIXED (*hz). False
 * for anything else (*policy untouched). */
bool n64c_cap_parse(const char *text,int *policy,u32 *hz);
/* What this window can present: unlock always, vsync only where the context's swap can wait for the display. */
void n64c_presentation_caps(N64cPresentationCaps *caps);
/* The presentation settings: validates, applies and reports in *effective what the window honours (vsync as the context
 * really has it, the cap resolved to its rate in cap_hz). On false (reason in error) nothing
 * changed. Vsync is the window's own and takes at once; unlock and the cap take at the next frame boundary of the live
 * loop (no picture group open), or at once before the loop runs. It never touches the guest's clock, and it never calls
 * back into the settings seam. */
bool n64c_presentation_apply(const N64cPresentationSettings *requested,N64cPresentationSettings *effective,char *error,size_t capacity);
/* The starting presentation after n64c_boot_frame_start: applied once (a refusal is logged and the defaults stay). */
void n64c_boot_frame_presentation(const N64cPresentationSettings *start);
/* The options screen's starting state (n64c_menu.h) after n64c_boot_frame_start of a live run: applies the starting window
 * mode and render scale, learns the display's sizes, reads back what the window really is and hands that, with the
 * presentation in force, to the settings host (n64c_settings_observe), then configures the screen with it. */
void n64c_boot_frame_options(const N64cOptions *now);
/* The real window and render target: mode, size and render scale together. On success *effective holds what the window
 * realized (read back: its drawable size and the render scale the target really has, which borderless and fullscreen make
 * differ from the request); on failure false with the reason in error and the window back as it was. */
bool n64c_display_apply(const N64cDisplaySettings *requested,N64cDisplaySettings *effective,char *error,size_t capacity);
int n64c_boot_frame_start(u32 width,u32 height,u32 ram_bytes,int audio_abi,u32 window_width,u32 window_height);
/* Run the original threads at the VI rate, showing every presentation, until the window closes (1) or
 * no original thread is left (0). Original stops exit the process. */
/* N64C_VIEW=<path>: the presented picture as a 320x240 BMP, replaced atomically at most every 66 ms of wall time
 * (N64C_VIEW_FPS, default 15; 60 once live input drives the run) and once more at a stop. N64C_AUDIO_OUT=<path>: see
 * n64c_audio_tap (at any speed; it keeps to real time by skipping). A run that live input takes over (n64c_tas.h)
 * drops to N64C_SPEED=1 from that retrace on; the guest's clock is per retrace, so its timing is unchanged. */
int n64c_boot_frame_live(void);
/* Wall-clock pacing only: N64C_SPEED=k (default 1) makes retrace n due n*1001/(60000*k) s after start, 0 never
 * waits. The guest's clock advances per retrace either way, so the game's own timing is the same at any speed.
 * n64c_retrace_due returns 0 when there is no wait; n64c_parse_speed returns 0 for anything but 0..1000. */
struct timespec;
int n64c_retrace_due(const struct timespec *start,u32 n,u32 speed,struct timespec *due);
int n64c_parse_speed(const char *text,u32 *speed);
/* The Expansion Pak switch (--pak on|off): RDRAM is 8 MB with the pak and 4 MB without; no text keeps the profile's
 * size. Returns 0 for any other text. */
int n64c_parse_pak(const char *text,u32 profile_bytes,u32 *ram);
/* 24-bit BMP of width x height pixels (0x00BBGGRR, top row first), the format N64C_VIEW writes; the tagged form
 * puts tag in the header's reserved word (bytes 6..9, little-endian). n64c_parse_view_fps reads N64C_VIEW_FPS
 * (1..120, default 15), 0 for anything else. */
int n64c_write_bmp(const char *path,const uint32_t *pixels,u32 width,u32 height);
int n64c_write_bmp_tagged(const char *path,const uint32_t *pixels,u32 width,u32 height,u32 tag);
int n64c_parse_view_fps(const char *text,u32 *fps);
int n64c_boot_frame_capture(const char *path,u32 retrace,u32 width,u32 height);
void n64c_boot_frame_close(void);
#endif
