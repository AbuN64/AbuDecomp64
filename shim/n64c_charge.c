/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The original-function entry charge (n64c_charge.h): the project's calibration and charged table, the entry hook. */
#include "n64c_charge.h"
#include "n64c_internal.h"
#include <stdio.h>
#include <stdlib.h>

/* Declared once by the project in its generated charged table (charged.py), linked into the image: the work scale as a
 * decimal string in guest ns per entry, and the addresses of the charged functions the link holds (a name the link does
 * not define is a null entry). Absent from a link that has no table: init refuses it. */
extern const char n64c_guest_ns_per_entry[] __attribute__((weak));
extern const void *const n64c_charged_functions[] __attribute__((weak));
extern const unsigned n64c_charged_count __attribute__((weak));

static const void *const no_functions[1];
const void *const *n64c_charged_fns = no_functions;
unsigned n64c_charged_mask;

__attribute__((noreturn)) static void refuse(const char *what)
{
    fprintf(stderr, "boot-missing: guest work charge: %s\n", what);
    fflush(stderr);
    _Exit(7);
}
/* The linker's own addresses of the charged functions (no symbol table to read, so one path for every target and
 * load bias): every defined entry goes into the address set. */
static unsigned resolve(const void **set, unsigned mask)
{
    unsigned count = 0;
    for (unsigned i = 0; i < n64c_charged_count; i++) {
        const void *fn = n64c_charged_functions[i];
        if (!fn)
            continue;
        unsigned a = (((unsigned)(uintptr_t)fn * 2654435761u) >> 7) & mask;
        while (set[a] && set[a] != fn)
            a = (a + 1) & mask;
        count += !set[a];
        set[a] = fn;
    }
    return count;
}
unsigned long long n64c_charge_init(void)
{
    if (!n64c_guest_ns_per_entry || !*n64c_guest_ns_per_entry)
        refuse("the project declares no work scale (n64c_guest_ns_per_entry)");
    unsigned long long fx = n64c_clock_parse_work(n64c_guest_ns_per_entry);
    if (!fx)
        refuse("the declared work scale is not a positive decimal number of ns per entry");
    if (!n64c_charged_functions || !n64c_charged_count)
        refuse("the project declares no charged functions");
    if (n64c_charged_fns == no_functions) { /* once: the table is the same for every reset */
        unsigned size = 1024;
        while (size < n64c_charged_count * 2)
            size <<= 1;
        const void **set = calloc(size, sizeof *set);
        if (!set)
            refuse("out of memory");
        if (!resolve(set, size - 1))
            refuse("no charged function is in the link");
        n64c_charged_mask = size - 1;
        n64c_charged_fns = set;
    }
    return fx;
}
/* The compiler's entry and exit calls of the original units. A project that traces (boot/trace.c) defines its own
 * pair, which calls n64c_charge_entry itself. */
__attribute__((weak, no_instrument_function)) void __cyg_profile_func_enter(void *fn, void *call_site)
{
    (void)call_site;
    n64c_charge_entry(fn);
}
__attribute__((weak, no_instrument_function)) void __cyg_profile_func_exit(void *fn, void *call_site)
{
    (void)fn;
    (void)call_site;
}
