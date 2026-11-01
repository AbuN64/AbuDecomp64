/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The tests' stand-in for the project's generated charged table (charged.py): a work scale too small to move any
 * test's clock (1 unit of 2^-20 ns) and one charged function. A test that checks the clock sets its own scale. */
const char n64c_guest_ns_per_entry[] = "0.000001";
__attribute__((used, noinline)) void n64c_test_charged_entry(void) {}
/* A charged name the link does not define is a null entry (the generated table's weak reference), not a link error. */
extern void n64c_test_charged_absent(void) __attribute__((weak));
const void *const n64c_charged_functions[] = {(const void *)&n64c_test_charged_absent, (const void *)&n64c_test_charged_entry};
const unsigned n64c_charged_count = 2;
