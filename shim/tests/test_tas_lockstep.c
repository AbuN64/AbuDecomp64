/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The lockstep per-poll point seam (n64c_hook.h): a 3-poll movie calls n64c_trace_poll_point once per guest poll, after
 * the pad is final, with the poll index, the VI count and the pad words as the guest read them. This test links its own
 * n64c_trace_poll_point in place of boot/trace.c's. */
#include "../n64c_cont.h"
#include "../n64c_hook.h"
#include <stdio.h>
#include <stdlib.h>

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)
static long seen_poll[8];
static unsigned seen_pad[8];
static int seen_pads[8], seen;

void n64c_trace_poll_point(long poll, unsigned vi, const unsigned *pad, int pads)
{
    (void)vi;
    if (seen < 8) {
        seen_poll[seen] = poll;
        seen_pad[seen] = pad[0];
        seen_pads[seen] = pads;
    }
    seen++;
}

static void live(int port, OSContPad *pad, void *user)
{
    (void)port; (void)user;
    *pad = (OSContPad){0};
}

int main(void)
{
    const char *movie = "n64c_test_tas_lockstep.movie";
    FILE *f = fopen(movie, "w");
    CHECK(f != NULL);
    if (!f) return 1;
    fputs("8000 0 0\n1000 -80 80\n0400 5 -5\n", f);
    fclose(f);
    setenv("N64C_TAS_PLAY", movie, 1);
    n64c_cont_set_input(live, NULL);
    OSContPad pads[MAXCONTROLLERS];
    for (int i = 0; i < 3; i++) {
        osContStartReadData(NULL);
        osContGetReadData(pads);
    }
    remove(movie);
    CHECK(seen == 3);
    CHECK(seen_poll[0] == 1 && seen_poll[1] == 2 && seen_poll[2] == 3);
    CHECK(seen_pad[0] == 0x80000000u);
    CHECK(seen_pad[1] == 0x1000b050u); /* stick -80 = 0xb0, 80 = 0x50 */
    CHECK(seen_pad[2] == 0x040005fbu);
    CHECK(seen_pads[0] >= 1);
    if (!fails) puts("n64c_tas_lockstep ok");
    return fails != 0;
}
