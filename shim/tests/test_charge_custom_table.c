/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The charged table of test_charge_custom.c: the custom-compiled unit's function is the one charged name. */
const char n64c_guest_ns_per_entry[] = "1000";
extern void n64c_test_custom_entry(void);
const void *const n64c_charged_functions[] = {(const void *)&n64c_test_custom_entry};
const unsigned n64c_charged_count = 1;
