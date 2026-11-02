# SPDX-License-Identifier: GPL-3.0-or-later
"""lockstep.py on synthetic oracle/port recordings, and the port recorder (trace.c, N64C_LOCKSTEP=1) on a fixture."""

import argparse
import collections
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest

parser = argparse.ArgumentParser()
parser.add_argument("--compiler", default="gcc")
parser.add_argument("--work", type=pathlib.Path, required=True)
parser.add_argument("--m32", action="store_true")
# bin/test runs this file as a script with its flags; pytest imports it and gets a scratch --work of its own.
args = (
    parser.parse_args()
    if __name__ == "__main__"
    else parser.parse_args(["--work", tempfile.mkdtemp(prefix="test_lockstep-")])
)
args.work.mkdir(parents=True, exist_ok=True)
ROOT = pathlib.Path(__file__).resolve().parent
os.environ.setdefault("N64C_PORT_DIR", str(ROOT / "fixtures"))  # a port facts directory when set
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("lockstep", ROOT / "lockstep.py")
ls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ls)
PAGES = 4
TRACE = (
    "order\taddress\tsymbol\n1\t0x80001000\tfn_one\n2\t0x80001100\tfn_two\tfn_two_new\n"
    "3\t0x80001200\tfn_three\n4\t0x80001300\tfn_four\n5\t0x80001400\tfn_five\n"
)
SYMBOLS = (
    "fn_one = 0x80001000; //\nfn_two_new = 0x80001100; //\nD_80000100 = 0x80000100; //\n"
    "D_80001800 = 0x80001800; //\nD_80002000 = 0x80002000; //\nD_80003000 = 0x80003000; //\n"
)


def write_dump(side_dir, poll, pages):
    """<side>/dump/<poll>/dump.bin as both recorders' dump replay writes it: pages {page: bytes}, the side's own order
    (port native, oracle canonical), each keyed by its canonical hash."""
    native = json.loads((side_dir / "lockstep.jsonl").read_text().splitlines()[0])["bytes"] == "native"
    folder = side_dir / ls.DUMP / str(poll)
    folder.mkdir(parents=True, exist_ok=True)
    data = b"".join(struct.pack("<IIQ", pg, 0, ls.page_hash(b, native=native)) + b for pg, b in sorted(pages.items()))
    (folder / ls.DUMP_FILE).write_bytes(data)


class Recorder:
    """Writes one side's lockstep.jsonl as the C recorders do, and a dump holding every page content it listed (as if
    a dump replay had written them all), so compare sees the bytes of every page."""

    def __init__(self, path, side):
        self.path = path
        path.mkdir(parents=True)
        self.side = side
        self.lines = [
            {
                "k": "meta",
                "side": side,
                "ram": PAGES * 4096,
                "page": 4096,
                "bytes": "canonical-be" if side == "oracle" else "native",
                "hash": "fnv1a64-le64",
            }
        ]
        self.last, self.dumped, self.n = {}, {}, 0

    def hit(self, key, ram, sp=0x80003F00, thr=1):
        pages = []
        for pg in range(PAGES):
            data = bytes(ram[pg * 4096 : (pg + 1) * 4096])
            h = ls.page_hash(data, native=self.side == "port")
            if self.n and self.last.get(pg) == h:
                continue
            self.last[pg] = h
            self.dumped.setdefault(h, (pg, data))
            pages.append([pg, f"{h:016x}"])
        rec = {"k": "hit", "n": self.n, "thr": thr, "pages": pages, "sp": f"{sp:08x}"}
        rec.update({"fn": key} if self.side == "oracle" else {"order": key})
        self.lines.append(rec)
        self.n += 1

    def call(self, key, i, words, sp, f12=0, f14=0):
        rec = {"k": "call", "i": i, "thr": 1, "sp": f"{sp:08x}", "a": [f"{w:08x}" for w in words]}
        if self.side == "oracle":
            rec.update(fn=key, f12=f"{f12:016x}", f14=f"{f14:016x}")
        else:
            rec.update(order=key)
        self.lines.append(rec)

    def ret(self, key, i, r, fp=None):
        rec = {"k": "ret", "i": i, "thr": 1, "r": [f"{w:08x}" for w in r]}
        if self.side == "oracle":
            rec.update(fn=key, f0=f"{fp or 0:016x}")
        else:
            rec.update(order=key, st0=None if fp is None else f"{fp:016x}")
        self.lines.append(rec)

    def close(self):
        (self.path / "lockstep.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.lines))
        # one dump file per distinct page content (a dump keys its records by page, so one page per file)
        for n, (pg, data) in enumerate(self.dumped.values()):
            write_dump(self.path, n, {pg: data})


class Ram:
    """The same guest memory in the oracle's canonical bytes and the port's native ones, written by type."""

    def __init__(self):
        self.oracle, self.port = bytearray(PAGES * 4096), bytearray(PAGES * 4096)

    def u32(self, address, value, port_value=None):
        at = address - 0x80000000
        self.oracle[at : at + 4] = value.to_bytes(4, "big")
        self.port[at : at + 4] = (value if port_value is None else port_value).to_bytes(4, "little")

    def u16(self, address, value):
        at = address - 0x80000000
        self.oracle[at : at + 2] = value.to_bytes(2, "big")
        self.port[at : at + 2] = value.to_bytes(2, "little")

    def u8(self, address, value):
        at = address - 0x80000000
        self.oracle[at] = self.port[at] = value

    def f64(self, address, value):
        at = address - 0x80000000
        self.oracle[at : at + 8] = struct.pack(">d", value)
        self.port[at : at + 8] = struct.pack("<d", value)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=args.work)
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)
        (self.path / "trace.tsv").write_text(TRACE)
        (self.path / "symbols.txt").write_text(SYMBOLS)
        self.oracle = Recorder(self.path / "oracle", "oracle")
        self.port = Recorder(self.path / "port", "port")
        self.ram = Ram()
        self.names = {1: "fn_one", 2: "fn_two", 3: "fn_three", 4: "fn_four", 5: "fn_five"}

    def both(self, key, sp=0x80003F00):
        self.oracle.hit(self.names[key], self.ram.oracle, sp)
        self.port.hit(key, self.ram.port)

    def run_compare(self, *extra):
        self.oracle.close()
        self.port.close()
        argv = [
            "compare",
            "--oracle",
            str(self.path / "oracle"),
            "--port",
            str(self.path / "port"),
            "--trace",
            str(self.path / "trace.tsv"),
            "--symbols",
            str(self.path / "symbols.txt"),
            "--pak",
            "5",
            *extra,
        ]
        return ls.compare(ls.build_parser().parse_args(argv))

    def test_all_memory_arguments_and_returns_are_listed_with_exact_acceptance(self):
        self.both(1)
        # More than both former display limits (10 text, 200 JSON).
        for i in range(210):
            self.ram.u32(0x80001800 + i * 4, 1, port_value=2)
        self.both(2)
        self.both(3)
        self.both(4)
        (self.path / "types.tsv").write_text("name\treturn\tparams\nfn_three\tint32\tint32,int32\n")
        self.oracle.call("fn_three", 0, [3, 4, 0, 0], 0x80003EC0)
        self.port.call(3, 0, [7, 8, 0, 0, 0, 0, 0, 0], 0xFFFFD000)
        self.oracle.ret("fn_three", 0, [5, 0])
        self.port.ret(3, 0, [9, 0])
        accepts = self.path / "accept.tsv"
        header = "\t".join(ls.ACCEPT_FIELDS) + "\n"
        accepts.write_text(
            header + "memory\t2\tfn_two\t0x80001800\t00000001/00000002\tfixture reason\tfixture review\n"
        )
        report = self.run_compare("--types", str(self.path / "types.tsv"), "--accept", str(accepts))
        self.assertEqual(len(report["memory"]), 210)
        self.assertEqual(len(report["calls"]), 3)
        self.assertEqual([r["why"].split()[0] for r in report["calls"]], ["arg0", "arg1", "return"])
        self.assertEqual(report["first"]["address"], "0x80001800")
        self.assertEqual(report["first_after_accepted"]["address"], "0x80001804")
        self.assertEqual(report["accepted"][0]["acceptance"]["reason"], "fixture reason")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ls.print_report(report)
        self.assertEqual(output.getvalue().count("lockstep: memory order"), 210)
        self.assertEqual(output.getvalue().count("lockstep: argument order"), 2)
        self.assertEqual(output.getvalue().count("lockstep: return order"), 1)
        self.assertIn("first after accepted rows: order 2", output.getvalue())
        self.assertIn("fixture reason", output.getvalue())

    def test_acceptance_advances_only_reviewed_exact_values(self):
        self.both(1)
        self.ram.u32(0x80001800, 1, port_value=2)
        self.both(2)
        self.both(3)
        self.ram.u32(0x80001804, 3, port_value=4)
        self.both(4)
        self.both(5)
        report = self.run_compare()
        review = dict(
            zip(
                ls.ACCEPT_FIELDS,
                ["memory", 2, "fn_two", "0x80001800", "00000001/00000002", "known", "reviewer"],
                strict=False,
            )
        )
        ls.apply_acceptances(report, [review], [1, 2, 3, 4, 5])
        self.assertEqual(report["matched_through"], 1)
        self.assertEqual(report["matched_through_after_accepted"], 3)
        self.assertEqual(report["first_after_accepted"]["order"], 4)
        review["detail"] = "00000001/00000009"
        ls.apply_acceptances(report, [review], [1, 2, 3, 4, 5])
        self.assertEqual(report["matched_through_after_accepted"], 1)
        self.assertEqual(len(report["unmatched_accepts"]), 1)
        self.assertEqual(report["accepted"], [])
        self.assertNotIn("acceptance", report["memory"][0])

    def test_accept_file_requires_reason_reviewer_and_unique_exact_rows(self):
        path = self.path / "accept.tsv"
        header = "\t".join(ls.ACCEPT_FIELDS) + "\n"
        good = "memory\t2\tfn_two\t0x80001800\t00000001/00000002\treason\treviewer\n"
        for bad in [
            good.replace("reason", ""),
            good.replace("reviewer", ""),
            good.replace("00000001/00000002", "*"),
            good + good,
            good.replace("0x80001800", "0x80001800-0x80001900"),
        ]:
            with self.subTest(row=bad):
                path.write_text(header + bad)
                with self.assertRaises(ValueError):
                    ls.read_acceptances(path)

    def test_layouts_that_agree_are_not_divergences(self):
        self.ram.u32(0x80000100, 0x12345678)
        self.ram.u16(0x80000104, 0xBEEF)
        self.ram.u16(0x80000106, 0x0102)
        for i, b in enumerate(b"TEXT"):
            self.ram.u8(0x80000108 + i, b)
        self.ram.u16(0x8000010C, 0xA1B2)
        self.ram.u8(0x8000010E, 7)
        self.ram.u8(0x8000010F, 9)
        self.ram.f64(0x80000110, 1.5)
        self.both(1)
        self.ram.u32(0x80001800, 0xDEADBEEF)
        self.both(2)
        self.both(3)
        report = self.run_compare()
        self.assertIsNone(report["first"], report)
        self.assertEqual(report["matched_through"], 3)

    def test_persistent_divergence_names_function_page_symbol_and_words(self):
        self.both(1)
        self.both(2)
        self.ram.u32(0x80001810, 5, port_value=6)
        self.both(3)
        self.both(4)
        self.both(5)
        report = self.run_compare()
        first = report["first"]
        self.assertEqual((first["kind"], first["order"], first["function"]), ("memory", 3, "fn_three"))
        self.assertEqual((first["page"], first["symbol"]), ("0x80001000", "D_80001800+0x10"))
        self.assertTrue(first["oracle"].startswith("00000005"), first)
        self.assertTrue(first["port"].startswith("00000006"), first)
        self.assertEqual(report["matched_through"], 2)

    def test_transient_and_stack_and_host_pointer_words_do_not_count(self):
        self.both(1)
        self.ram.u32(0x80001900, 1, port_value=2)  # differs at one point only, then agrees
        self.both(2)
        self.ram.u32(0x80001900, 3)
        self.ram.u32(0x80003E00, 1, port_value=0)  # inside the oracle's guest stack page
        self.ram.u32(0x80001910, 0x80002100, port_value=0x5655A000)  # a shim's host pointer
        self.both(3)
        self.both(4)
        self.both(5)
        report = self.run_compare()
        self.assertIsNone(report["first"], report)
        self.assertEqual(report["transient_words"], 1)
        self.assertEqual(report["shim_words"], 1)
        self.assertEqual(report["matched_through"], 5)

    def test_ignore_rows_and_oracle_volatility_mask_words(self):
        (self.path / "ignore.tsv").write_text("D_80001800\tshim-owned queue\n")
        self.both(1)
        self.ram.u32(0x80001800, 1, port_value=2)
        self.both(2)
        self.both(3)
        report = self.run_compare("--ignore", str(self.path / "ignore.tsv"))
        self.assertIsNone(report["first"], report)
        self.assertIn(["0x80001800", "0x80002000", "shim-owned queue"], report["masked"])

    def test_return_divergence_needs_a_type_and_comes_first(self):
        (self.path / "types.tsv").write_text("name\treturn\tparams\nfn_two\tint32\tint32,ptr,float\nfn_three\tvoid\t\n")
        self.both(1)
        self.both(2)
        self.both(3)
        self.ram.u32(0x80001810, 5, port_value=6)
        self.both(4)
        self.both(5)
        # fn_two(7, &stack, 2.5f): equal; returns 1 vs garbage
        self.oracle.call("fn_two", 0, [7, 0x80003E80, 0x40200000, 0], 0x80003EC0, f12=0)
        self.port.call(2, 0, [7, 0xFFFFD010, 0x40200000, 0, 0, 0, 0, 0], 0xFFFFD000)
        self.oracle.ret("fn_two", 0, [1, 0])
        self.port.ret(2, 0, [0x0806B2C4, 0])
        self.oracle.ret("fn_three", 0, [9, 0])  # void: never compared
        report = self.run_compare("--types", str(self.path / "types.tsv"))
        first = report["first"]
        self.assertEqual((first["kind"], first["order"], first["function"]), ("call", 2, "fn_two"), report)
        self.assertEqual(first["why"], "return int32 0x00000001 vs port 0x0806b2c4")
        self.assertEqual(report["matched_through"], 1)

    def test_float_and_double_arguments_follow_o32(self):
        self.assertEqual(ls.o32_slots(["float", "float", "int32"]), ([("f", 12), ("f", 14), ("a", 2)], [0, 4, 8]))
        self.assertEqual(ls.o32_slots(["int32", "double"]), ([("a", 0), ("a", 2)], [0, 4]))
        self.assertEqual(
            ls.o32_slots(["double", "int32", "int32", "int32"]), ([("f", 12), ("a", 2), ("a", 3), None], [0, 8, 12, 16])
        )
        matcher = ls.Matcher(PAGES * 4096, ([], {}), [])
        oc = {
            "a": ["0", "0", "0", "0"],
            "f12": f"{struct.unpack('<Q', struct.pack('<d', 2.0))[0]:016x}",
            "f14": "0",
            "sp": "80003f00",
        }
        lo, hi = struct.unpack("<II", struct.pack("<d", 2.0))
        pc = {"a": [f"{lo:08x}", f"{hi:08x}"] + ["0"] * 6, "sp": "ffffd000"}
        self.assertEqual(ls.compare_call(matcher, ["double"], False, oc, pc), [])
        pc["a"][0] = "00000001"
        self.assertIn("arg0 double", ls.compare_call(matcher, ["double"], False, oc, pc)[0])

    def test_function_pointers_match_by_original_address(self):
        matcher = ls.Matcher(
            PAGES * 4096,
            ls.read_symbols(self.path / "symbols.txt"),
            [(0x08049000, 0x40, "fn_one", False), (0x08049100, 0x40, "fn_two_new", True)],
        )
        self.assertTrue(matcher.same_function(0x80001100, 0x08049100))
        self.assertFalse(matcher.same_function(0x80001000, 0x08049100))
        page_o, page_p = bytearray(4096), bytearray(4096)
        page_o[0:4] = (0x80001000).to_bytes(4, "big")
        page_p[0:4] = (0x08049000).to_bytes(4, "little")
        self.assertEqual(matcher.page_diff(bytes(page_o), bytes(page_p)), {})
        self.assertTrue(matcher.is_local(["fn_two", "fn_two_new"]))

    def test_native_data_pointers_match_by_original_address(self):
        matcher = ls.Matcher(
            PAGES * 4096,
            ls.read_symbols(self.path / "symbols.txt"),
            [],
            [(0x0805A000, 0x100, "D_80001800", True), (0x0805B000, 0x20, "rw_table.12", False)],
        )
        self.assertTrue(matcher.same_address(0x80001808, 0x0805A008))
        self.assertTrue(matcher.same_address(0x80001900, 0x0805A100))  # one past the end
        self.assertFalse(matcher.same_address(0x80001808, 0x0805A00C))
        self.assertIsNone(matcher.port_data(0x0805B000))  # no original address for the name
        self.assertFalse(matcher.host_pointer(0x80001808, 0x0805A008))

    def test_calls_without_a_prototype_are_not_compared(self):
        matcher = ls.Matcher(PAGES * 4096, ([], {}), [])
        oc = {"a": ["1", "2", "3", "4"], "f12": "0", "f14": "0", "sp": "80003f00"}
        pc = {"a": ["9"] * 8, "sp": "ffffd000"}
        self.assertEqual(ls.compare_call(matcher, None, False, oc, pc), [])
        self.assertIn("arg0 int32", ls.compare_call(matcher, ["int32"], False, oc, pc)[0])

    def test_returned_stack_pointers_and_other_threads_stacks_match(self):
        matcher = ls.Matcher(PAGES * 4096, ([], {}), [])
        oret, pret = {"r": ["80003e80", "0"]}, {"r": ["ffffd010", "0"]}
        self.assertIsNone(ls.compare_return(matcher, "ptr", oret, pret, 0x80003EC0, 0xFFFFD000))
        self.assertIsNotNone(ls.compare_return(matcher, "ptr", oret, pret))
        matcher.set_stacks({1: [0x80003EC0], 2: [0x80002F00]}, {7: [0xFFFFD000], 8: [0xB92FF000]})
        self.assertTrue(ls.same_word(matcher, "ptr", 0x80002E80, 0xB92FFF38, 0x80003EC0, 0xFFFFD000))
        self.assertFalse(ls.same_word(matcher, "ptr", 0x80001000, 0xB92FFF38, 0x80003EC0, 0xFFFFD000))

    def test_float_rounding_is_listed_not_counted(self):
        self.assertTrue(ls.near_float(0x441DE734, 0x441DE733))
        self.assertFalse(ls.near_float(0x441DE734, 0x441DE740))
        self.assertFalse(ls.near_float(5, 6))  # small integers are not floats
        self.both(1)
        self.ram.u32(0x80001810, 0x3F59E2CA, port_value=0x3F59E2C9)
        self.both(2)
        self.both(3)
        self.both(4)
        report = self.run_compare()
        self.assertIsNone(report["first"], report)
        self.assertEqual(report["fp_ulp_words"], 1)

    def test_setup_differences_count_once_the_game_writes_them(self):
        self.ram.u32(0x80001820, 0, port_value=0x77)  # the shim wrote it before the first game function
        self.ram.u32(0x80001830, 0, port_value=0x55)
        self.both(1)
        self.both(2)
        self.ram.u32(0x80001830, 3, port_value=4)  # the game writes it and the port disagrees
        self.both(3)
        self.both(4)
        self.both(5)
        report = self.run_compare()
        self.assertEqual(report["setup_words"], 1)
        self.assertEqual((report["first"]["order"], report["first"]["address"]), (3, "0x80001830"))
        self.assertEqual(report["matched_through"], 2)

    def test_rom_boot_copy_is_masked(self):
        rom = bytearray(0x1000 + 0x100000)
        rom[0:4] = bytes.fromhex("80371240")
        rom[0x1000 + 0x1400 : 0x1000 + 0x1404] = bytes.fromhex("27bdffc8")  # IPL3 puts it at 0x80001800
        (self.path / "rom.z64").write_bytes(rom)
        self.ram.oracle[0x1800:0x1804] = bytes.fromhex("27bdffc8")
        self.ram.u32(0x80001900, 2, port_value=1)  # differs after the first point: counted
        self.both(1)
        self.both(2)
        self.ram.u32(0x80001904, 2, port_value=1)
        self.both(3)
        self.both(4)
        self.both(5)
        report = self.run_compare("--rom", str(self.path / "rom.z64"))
        self.assertEqual(report["boot_copy_words"], 1)
        self.assertEqual(report["setup_words"], 1)
        self.assertEqual(report["first"]["address"], "0x80001904")
        v64 = bytearray(rom)
        v64[0::2], v64[1::2] = rom[1::2], rom[0::2]
        (self.path / "rom.v64").write_bytes(v64)
        self.assertEqual(ls.read_boot_copy(self.path / "rom.v64"), ls.read_boot_copy(self.path / "rom.z64"))

    def test_aux_info_kinds(self):
        typedefs = {
            "s32": "int",
            "f32": "float",
            "u8": "unsigned char",
            "Vec": "struct",
            "OSTime": "u64",
            "u64": "unsigned long long",
        }
        text = (
            "/* a.c:3:NF */ extern s32 fn_one (f32 x, u8 *p, OSTime t, ...); /* (x, p, t) */\n"
            "/* a.c:9:OF */ extern void fn_two (); /* () */\n"
            "/* a.c:12:NC */ extern Vec fn_three (Vec, int (*)(void));\n"
        )
        out = {}
        ls.parse_aux(text, {"fn_one", "fn_two", "fn_three"}, typedefs, out)
        self.assertEqual(out["fn_one"], ("int32", ["float", "ptr", "int64", "var"]))
        self.assertEqual(out["fn_two"], ("void", None))
        self.assertEqual(out["fn_three"], ("agg", ["agg", "ptr"]))


FIXTURE_GAME = r"""
#include <setjmp.h>
#include <stdio.h>
#define NI __attribute__((noinline))
extern unsigned char *ram_base;
static jmp_buf escape;
NI int add(int a, int b) { ram_base[0x1000] = (unsigned char)(a + b); return a + b; }
NI float half(float x) { return x * 0.5f; }
NI long long wide(int a) { return ((long long)a << 32) | 5; }
NI int depth(int n) { return n ? depth(n - 1) + 1 : add(n, 40); }
NI void leave(int n) { if (n > 0) leave(n - 1); if (n <= 0) longjmp(escape, 1); }
static __attribute__((always_inline)) inline int bump(int x) { return x + 3; }
static int (*volatile bump_ptr)(int) = bump;
NI int inline_parent(int x) { return bump(x * 2) + x; }
/* Mock the hooks emitted for an inlined recursive body: they stay inside
 * this symbol and reuse its physical frame. Real recursion is tested by depth. */
extern void __cyg_profile_func_enter(void *, void *);
extern void __cyg_profile_func_exit(void *, void *);
NI int recursive_inline(int value) {
    void *site = __builtin_return_address(0);
    __cyg_profile_func_enter((void *)recursive_inline, site);
    int result = value + 3;
    __cyg_profile_func_exit((void *)recursive_inline, site);
    return result;
}
NI int run(void) {
    printf("add=%d\n", add(2, 3));
    printf("half=%g\n", (double)half(3.0f));
    printf("wide=%llx\n", wide(9));
    printf("depth=%d\n", depth(3));
    if (!setjmp(escape)) leave(2);
    printf("after=%d\n", add(1, 1));
    int sum = 0;
    for (int i = 0; i < 12; i++) sum += inline_parent(i);
    printf("inline=%d indirect=%d\n", sum, bump_ptr(7));
    sum = 0;
    for (int i = 0; i < 12; i++) sum += recursive_inline(i);
    printf("recursive_inline=%d\n", sum);
    return 0;
}
"""
FIXTURE_TABLE = r"""#include "trace.h"
const N64cTraceEntry n64c_trace_entries[] = {
    {1, 0x80001000u, 0, {"run"}}, {2, 0x80001100u, 0, {"add"}}, {3, 0x80001200u, 0, {"half"}},
    {4, 0x80001300u, 0, {"wide"}}, {5, 0x80001400u, 0, {"depth"}}, {6, 0x80001500u, 0, {"leave"}},
    {7, 0x80001600u, 0, {"bump"}}, {8, 0x80001700u, 0, {"inline_parent"}}, {9, 0x80001800u, 0, {"recursive_inline"}}};
const unsigned n64c_trace_count = 9;
/* This fixture executes original bodies only; no native bindings. */
const N64cShimEntry n64c_shim_entries[] = {{0, 0}};
const unsigned n64c_shim_count = 0;
"""
FIXTURE_DRIVER = r"""#include <stdio.h>
#include <string.h>
static unsigned char ram[0x400000] __attribute__((aligned(4096)));
unsigned char *ram_base = ram;
void *n64c_memory_ram(unsigned physical, unsigned bytes) { return physical + bytes <= sizeof ram ? ram + physical : 0; }
extern void n64c_trace_init(void);
extern void n64c_trace_summary(void);
extern void n64c_trace_reach_tick(long poll);
extern int run(void);
extern void n64c_trace_poll_point(long poll, unsigned vi, const unsigned *pad, int pads);
int main(void) { memcpy(ram + 0x2000, "\x78\x56\x34\x12", 4); n64c_trace_init();
    const unsigned pads[2][2] = {{0x80000000u, 0}, {0x1000b050u, 0x040005fbu}};
    n64c_trace_reach_tick(1); n64c_trace_poll_point(1, 2, pads[0], 2); int r = run();
    n64c_trace_reach_tick(2); n64c_trace_poll_point(2, 4, pads[1], 2);
    n64c_trace_poll_point(3, 6, pads[1], 1); n64c_trace_summary(); fflush(stdout); return r; }
"""


ORACLE_POLLS = ROOT / "fixtures" / "lockstep-poll-oracle"  # CakeTaste64 lockstep.cpp (4be5381; its meta `frame`
# `poll` and the call's poll-index stamp 1 as lockstep.cpp writes them since rw1-rec), its 3-poll driver,
# hashes only, and its dump replay (N64R_LOCKSTEP_DUMP=2:1,2): dump/2/dump.bin holds pages 1 and 2 at poll 2
PORT_PADS = ["80000000", "1000b050", "040005fb"]  # the port row's n64c_tas_lockstep movie, polls 1..3


def native(page):
    """The oracle's canonical page as the port holds it: each 32-bit word in host (little-endian) order."""
    return b"".join(page[i : i + 4][::-1] for i in range(0, len(page), 4))


def oracle_pages(poll):
    """The oracle fixture's RDRAM at a poll, canonical, from what its driver stores: 0x11223344 at 0x80002000 before
    poll 1, the byte 0xab at 0x80001000 before poll 2. The record's hashes prove it (oracle_state)."""
    ram = {pg: bytes(4096) for pg in range(4)}
    ram[2] = bytes.fromhex("11223344") + bytes(4092)
    if poll >= 2:
        ram[1] = b"\xab" + bytes(4095)
    return ram


def oracle_state(oracle, poll):
    """oracle_pages(poll), checked page by page against the hashes the record holds at that poll."""
    current = {}
    for p, listing in oracle.poll_pages():
        if p <= poll:
            current.update((pg, int(h, 16)) for pg, h in listing)
    pages = oracle_pages(poll)
    assert {pg: ls.page_hash(b) for pg, b in pages.items()} == current, poll
    return pages


def port_from_oracle(out, edit=None, dump=None):
    """A port run of the real oracle slice: the lines trace.c writes (meta, poll, call/ret) over the oracle's pages in
    native order, hashed by the port's canonical hash. edit(record, pages, current) changes one poll before writing;
    dump (poll, pages) also writes what the port's dump replay writes there: those pages, native."""
    oracle = ls.Side(ORACLE_POLLS, lambda r: None)
    shutil.rmtree(out, ignore_errors=True)  # a dump an earlier run left there would hold bytes this run has not dumped
    out.mkdir(parents=True)
    meta = {"k": "meta", "side": "port", "ram": oracle.meta["ram"], "page": 4096, "bytes": "native",
            "hash": "fnv1a64-le64", "calls": 8, "trace": 0, "watch": "00000000"}  # fmt: skip
    lines, current, held = [json.dumps(meta, separators=(",", ":"))], {}, {}
    oracle_pages = dict(oracle.poll_pages())
    for poll in sorted(oracle.polls):
        r = dict(oracle.polls[poll])
        state = oracle_state(oracle, poll)
        pages = {pg: native(state[pg]) for pg, _ in oracle_pages[poll]}
        if edit:
            edit(r, pages, current)
        listed = []
        for pg in sorted(pages):
            h = ls.page_hash(pages[pg], native=True)
            held[pg] = pages[pg]
            if current.get(pg) == h:
                continue
            current[pg] = h
            listed.append([pg, f"{h:016x}"])
        r["pages"] = listed
        lines.append(json.dumps(r, separators=(",", ":")))
        if dump and dump[0] == poll:
            dumped = {pg: held.get(pg, bytes(4096)) for pg in dump[1]}
    for line in (ORACLE_POLLS / "lockstep.jsonl").read_text().splitlines():
        if json.loads(line)["k"] in ("call", "ret"):
            lines.append(line)
    (out / "lockstep.jsonl").write_text("\n".join(lines) + "\n")
    if dump:
        write_dump(out, dump[0], dumped)
    return out


def poll_lines(directory):
    return [x for x in (directory / "lockstep.jsonl").read_text().splitlines() if '"k":"poll"' in x]


class RunRecordTests(unittest.TestCase):
    """CONTRACTS.md Run record: a run of the real oracle slice and its port, matched with --run, checked by check."""

    def run_record(self, name):
        run = args.work / name
        shutil.rmtree(run, ignore_errors=True)
        (run / ls.RUN_LOCKSTEP).mkdir(parents=True)
        shutil.copytree(ORACLE_POLLS, run / ls.RUN_LOCKSTEP / "oracle")
        port = port_from_oracle(run / ls.RUN_LOCKSTEP / "port")
        calls = [json.dumps({"k": "meta", "side": "port", "mode": "calls", "first": 16})]
        calls += [x for x in (port / "lockstep.jsonl").read_text().splitlines() if '"o":' in x]  # its whole-movie calls
        (port / ls.RUN_CALLS).write_text("\n".join(calls) + "\n")
        (run / ls.RUN_RECORD).write_text(json.dumps({"id": name, "status": "done"}))
        (run / "movie.raw").write_text("0 0 0\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ls.main(["match", "--run", str(run)]), 0)
        return run

    def check(self, run):
        with contextlib.redirect_stdout(io.StringIO()) as text:
            code = ls.main(["check", str(run)])
        return code, text.getvalue()

    def test_matched_run_holds_field_by_field(self):
        run = self.run_record("record-holds")
        self.assertEqual(json.loads((run / "lockstep" / "match.json").read_text())["first"], None)
        self.assertEqual(self.check(run), (0, f"lockstep check: {run}: holds\n"))

    def test_each_broken_field_is_named(self):
        run = self.run_record("record-breaks")
        meta = run / "lockstep" / "port" / "lockstep.jsonl"
        lines = meta.read_text().splitlines()
        head = json.loads(lines[0])
        head["bytes"] = "canonical-be"
        poll = json.loads(lines[1])
        poll["pad"], poll["pages"] = ["xyz"], [[0, "00000000000000aa"]]
        meta.write_text("\n".join([json.dumps(head), json.dumps(poll), *lines[2:]]) + "\n")
        report = run / "lockstep" / "match.json"
        report.write_text(json.dumps({"polls": {}, "matched_through_poll": "1", "first": None, "divergences": {},
                                      "dump": {"poll": 2}}))  # fmt: skip
        oracle = run / "lockstep" / "oracle"
        write_dump(oracle, 3, {3: b"\x01" + bytes(4095)})  # a replay that did not reach the recorded state
        data = (oracle / "dump" / "2" / "dump.bin").read_bytes()
        (oracle / "dump" / "2" / "dump.bin").write_bytes(data[:16] + b"\xff" + data[17:])  # bytes off their hash
        write_dump(oracle, 9, {0: bytes(4096)})  # a dump of a poll the record never reached
        code, text = self.check(run)
        self.assertEqual(code, 1)
        for field in ("meta.bytes", "pad:", "match.json polls", "matched_through_poll", "match.json divergences",
                      "match.json dump", "dump 3 page 3", "the replay is not the recorded run",
                      "dump 2 page 1: its bytes do not hash", "dump 9: no such poll"):  # fmt: skip
            self.assertIn(field, text)

    def test_run_without_inputs_or_sides_refuses(self):
        run = args.work / "record-empty"
        shutil.rmtree(run, ignore_errors=True)
        run.mkdir()
        self.assertIn("movie.raw: missing", self.check(run)[1])
        (run / "movie.raw").write_text("")
        code, text = self.check(run)
        self.assertEqual(code, 1)
        self.assertIn("lockstep/oracle:", text)
        self.assertIn("lockstep/match.json:", text)

    def test_match_run_refuses_mixed_addressing(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ls.main(["match", "--run", "x", "--port", "y"]), 2)
            self.assertEqual(ls.main(["match", "--port", "y"]), 2)
        self.assertIn("--run names the sides", err.getvalue())


class MatchTests(unittest.TestCase):
    def run_match(self, port, *extra):
        report = args.work / "match.json"
        with contextlib.redirect_stdout(io.StringIO()) as text:
            code = ls.main(["match", "--oracle", str(ORACLE_POLLS), "--port", str(port), "--json", str(report), *extra])
        return code, json.loads(report.read_text()), text.getvalue()

    def test_real_oracle_page_hashes_the_same_as_the_port_holding_it_natively(self):
        oracle = ls.Side(ORACLE_POLLS, lambda r: None)
        h = int(dict(oracle.poll_pages())[1][2][1], 16)
        page = oracle.bytes(h)  # page 2 is unchanged from poll 1 to the dump at poll 2
        self.assertEqual(page[:4], bytes.fromhex("11223344"))
        self.assertEqual(ls.page_hash(page), h)
        self.assertEqual(ls.page_hash(native(page), native=True), h)
        self.assertNotEqual(ls.page_hash(native(page)), h)  # the old port hash over native bytes never met the oracle's

    def test_real_oracle_record_is_hashes_only_and_its_dump_holds_just_the_named_pages(self):
        # A whole-movie store of every distinct page was gigabytes (pp-1p-match: 10,491,587,072 B): the record keeps
        # hashes only, and the dump replay wrote the two pages it was asked for at poll 2, nothing else.
        self.assertEqual(sorted(p.name for p in ORACLE_POLLS.iterdir()), ["dump", "lockstep.jsonl"])
        self.assertEqual([p.name for p in (ORACLE_POLLS / "dump").iterdir()], ["2"])
        self.assertEqual([p.name for p in (ORACLE_POLLS / "dump" / "2").iterdir()], ["dump.bin"])
        oracle = ls.Side(ORACLE_POLLS, lambda r: None)
        self.assertEqual(sorted(oracle.dumps), [2])
        self.assertEqual(sorted(oracle.dumps[2]), [1, 2])
        self.assertEqual(oracle.bad_dumps, [])
        state = oracle_state(oracle, 2)
        for pg, h in oracle.dumps[2].items():
            self.assertEqual(oracle.bytes(h), state[pg])
        self.assertIsNone(oracle.bytes(ls.page_hash(b"\x01" + bytes(4095))))  # no dump holds it: no bytes
        code, report, text = self.run_match(port_from_oracle(args.work / "match-hashes"))
        self.assertEqual(code, 0, text)
        self.assertIsNone(report["dump"])  # equal hashes need no bytes

    def test_page_without_bytes_is_the_divergence_and_the_dump_names_it_then_resolves_it_word_by_word(self):
        def edit(r, pages, current):
            if r["poll"] >= 2:
                pages[1] = native(b"\xac" + bytes(4095))  # the game stored 0xac where the original stored 0xab

        port = port_from_oracle(args.work / "match-dump", edit)
        code, report, _ = self.run_match(port)
        self.assertEqual(code, 1)
        hashes = [f"{ls.page_hash(b + bytes(4095)):016x}" for b in (b"\xab", b"\xac")]
        self.assertEqual(report["divergences"]["memory"],
                         [{"poll": 2, "address": "80001000", "symbol": "0x80001000", "class": "page",
                           "oracle": hashes[0], "port": hashes[1]}])  # fmt: skip
        self.assertEqual(report["dump"], {"poll": 2, "pages": [1]})
        with contextlib.redirect_stdout(io.StringIO()) as text:
            run = args.work / "match-dump-run"
            shutil.rmtree(run, ignore_errors=True)
            (run / ls.RUN_LOCKSTEP).mkdir(parents=True)
            shutil.copytree(ORACLE_POLLS, run / ls.RUN_LOCKSTEP / "oracle")
            shutil.copytree(port, run / ls.RUN_LOCKSTEP / "port")
            self.assertEqual(ls.main(["pages", "--run", str(run), "--poll", "2"]), 0)
            self.assertEqual(ls.main(["pages", "--run", str(run), "--poll", "1"]), 0)
        self.assertEqual([json.loads(x) for x in text.getvalue().splitlines()], [{"poll": 2, "pages": [1]}, None])
        # the port's dump replay of poll 2 (the oracle's is the fixture's): the page compares word by word
        port_from_oracle(port, edit, dump=(2, [1]))
        code, report, _ = self.run_match(port)
        self.assertEqual(code, 1)
        self.assertEqual(report["divergences"]["memory"],
                         [{"poll": 2, "address": "80001000", "symbol": "0x80001000", "class": "value",
                           "oracle": "ab000000", "port": "ac000000"}])  # fmt: skip
        self.assertIsNone(report["dump"])

    def test_matching_movie_writes_equal_poll_lines_and_matches_through_the_last_poll(self):
        port = port_from_oracle(args.work / "match-same")
        self.assertEqual(poll_lines(port), poll_lines(ORACLE_POLLS))
        code, report, text = self.run_match(port)
        self.assertEqual(code, 0, text)
        self.assertEqual(report["polls"], {"oracle": 3, "port": 3, "common": 3})
        self.assertEqual(report["matched_through_poll"], 3)
        self.assertIsNone(report["first"])
        self.assertEqual(report["divergences"], {"input": [], "vi": [], "calls": [], "memory": []})

    def test_divergences_listed_per_class_in_poll_order_with_the_first_and_matched_through(self):
        def edit(r, pages, current):
            r["pad"] = [PORT_PADS[r["poll"] - 1], *r["pad"][1:]]
            if r["poll"] >= 2:
                r["vi"] += 1  # one retrace more from poll 2 on
            if r["poll"] == 2:
                pages[0] = b"\x2a\x00\x00\x00" + bytes(4092)  # a word the game wrote differently, kept to the end

        port = port_from_oracle(args.work / "match-diverge", edit, dump=(2, [0]))  # the oracle's page 0 is zero
        code, report, text = self.run_match(port, "--persist", "1")
        self.assertEqual(code, 1)
        d = report["divergences"]
        self.assertEqual([(x["poll"], x["port"][0]) for x in d["input"]], [(1, "80000000"), (2, "1000b050"),
                                                                           (3, "040005fb")])  # fmt: skip
        self.assertEqual(d["vi"], [{"poll": 2, "oracle": 4, "port": 5, "offset": 1}])
        self.assertEqual([(x["poll"], x["address"], x["oracle"], x["port"]) for x in d["memory"]],
                         [(2, "80000000", "00000000", "0000002a")])  # fmt: skip
        self.assertEqual(report["first"]["class"], "input")
        self.assertEqual(report["matched_through_poll"], 0)
        self.assertIn("matched through poll 0", text)

    def test_poll_only_one_side_reached_is_an_input_divergence(self):
        port = port_from_oracle(args.work / "match-short")
        lines = (port / "lockstep.jsonl").read_text().splitlines()
        (port / "lockstep.jsonl").write_text("\n".join(x for x in lines if '"poll":3' not in x) + "\n")
        code, report, _ = self.run_match(port)
        self.assertEqual(code, 1)
        self.assertEqual(
            report["divergences"]["input"], [{"poll": 3, "oracle": ["0010ff01", "00200000"], "port": None}]
        )
        self.assertEqual(report["matched_through_poll"], 2)

    def test_call_in_another_poll_is_a_calls_divergence(self):
        port = port_from_oracle(args.work / "match-call")
        text = (port / "lockstep.jsonl").read_text().replace('"frame":1', '"frame":2')
        (port / "lockstep.jsonl").write_text(text)
        code, report, _ = self.run_match(port)
        self.assertEqual(code, 1)
        self.assertEqual([(x["fn"], x["field"], x["oracle"], x["port"]) for x in report["divergences"]["calls"]],
                         [("f", "poll", 1, 2)])  # fmt: skip

    def test_side_without_polls_is_bad_input(self):
        port = port_from_oracle(args.work / "match-nopoll")
        lines = (port / "lockstep.jsonl").read_text().splitlines()
        (port / "lockstep.jsonl").write_text("\n".join(x for x in lines if '"k":"poll"' not in x) + "\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ls.main(["match", "--oracle", str(ORACLE_POLLS), "--port", str(port)]), 2)
        self.assertIn("poll lines", err.getvalue())


# pp-1p-match (rig play 20261102T055254Z, 29,779 common polls, then a 10.5 GB oracle page store): its first polls cut to
# the pages that diverge most, every differing page's bytes held as one dump per side, and the memory divergences
# lockstep.py listed for that cut before match_memory went incremental. The whole play once took over 20 minutes in
# match_memory (every RAM page and every differing word revisited at every poll); each poll now costs only the pages it
# lists.
PP_SLICE = ROOT / "fixtures" / "lockstep-pp-1p-match"


class MatchSliceTests(unittest.TestCase):
    def test_real_pp_1p_match_slice_lists_the_same_memory_divergences_reading_each_listed_page_once(self):
        oracle, port = ls.Side(PP_SLICE / "oracle", lambda r: None), ls.Side(PP_SLICE / "port", lambda r: None)
        reads = []
        for side in (oracle, port):
            self.assertEqual(side.bad_dumps, [])
            side.bytes = lambda h, read=side.bytes: reads.append(h) or read(h)
        matcher = ls.Matcher(min(oracle.meta["ram"], port.meta["ram"]), ([], {}), [])
        polls = sorted(set(oracle.polls) & set(port.polls))
        memory = ls.match_memory(matcher, oracle, port, polls, 2)
        oracle_pages, port_pages = dict(oracle.poll_pages()), dict(port.poll_pages())
        self.assertEqual(memory, json.loads((PP_SLICE / "memory.json").read_text()))
        listed, oh, ph = 0, {}, {}  # the page pairs that differ among the pages each poll lists
        for poll in polls:
            oh.update(oracle_pages[poll])
            ph.update(port_pages[poll])
            pages = {pg for pg, _ in oracle_pages[poll] + port_pages[poll]}
            listed += sum(oh.get(pg) != ph.get(pg) for pg in pages)
        self.assertEqual(len(reads), 2 * listed)  # each listed page once, never every differing page at every poll


# boot-idle on rig 11-02 (measure m-580e: RageWarsEnhanced 580ef0a against the live oracle), both sides cut to polls 0
# and 1: meta, poll lines (pages emptied), whole-movie call lines. The oracle there stamped its calls with the retrace
# count, kept 8 calls per caller where the port kept 16, and recorded 4 o32 register words where the port records 8
# i386 stack words; keyed by (fn, caller, o) the old match listed 4342 poll, 4349 args and 3038 missing rows for this
# slice (the whole record: 8074, 8081, 7148), every one a recorder artifact. The five calls left really do fall in
# another poll.
BOOT_SLICE = ROOT / "fixtures" / "lockstep-boot-idle-580e.tar.xz"


class MatchRecordSliceTests(unittest.TestCase):
    def test_real_boot_idle_slice_lists_only_the_calls_that_fall_in_another_poll(self):
        work = args.work / "boot-slice"
        shutil.rmtree(work, ignore_errors=True)
        with tarfile.open(BOOT_SLICE) as archive:
            archive.extractall(work, filter="data")
        sides = work / "lockstep-boot-idle-580e"
        report = args.work / "boot-slice.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = ls.main(["match", "--oracle", str(sides / "oracle"), "--port", str(sides / "port"),
                            "--json", str(report)])  # fmt: skip
        self.assertEqual(code, 1)
        data = json.loads(report.read_text())
        calls = data["divergences"]["calls"]
        self.assertEqual(collections.Counter(x["field"] for x in calls), {"poll": 5})  # no args, no missing
        self.assertEqual([(x["fn"], x["i"], x["oracle"], x["port"]) for x in calls],
                         [("Cinema_IsSequenceActive", 3, 0, 1)] +
                         [("Node_FindWidgetByResourceId", i, 0, 1) for i in range(4, 8)])  # fmt: skip
        self.assertEqual(data["keep"], {"oracle": 8, "port": 16})
        # the oracle's native-on-the-port calls are listed, never counted: shim-bound, unlinked or unframed there
        self.assertEqual(len(data["unrecorded_on_port"]), 114)
        self.assertIn("Audiomgr_RenderAudioFrame", data["unrecorded_on_port"])
        oracle = ls.Side(sides / "oracle", lambda r: None)
        poll_of = {key: r["poll"] for key, r in oracle.movie_calls.items()}
        self.assertNotIn("frame", oracle.meta)  # the old oracle: retrace stamps, so the poll is its stream position
        self.assertEqual(poll_of[("Cinema_IsSequenceActive", 3)], 0)
        self.assertGreater(oracle.movie_calls[("Cinema_IsSequenceActive", 3)]["frame"], 1)  # a retrace, not a poll


@unittest.skipUnless(args.m32, "the argument words and the return trampoline are i386")
class PortRecorderTests(unittest.TestCase):
    def test_fixture_records_points_calls_and_returns_and_still_behaves(self):
        with tempfile.TemporaryDirectory(dir=args.work) as tmp:
            path = pathlib.Path(tmp)
            (path / "game.c").write_text(FIXTURE_GAME)
            (path / "table.c").write_text(FIXTURE_TABLE)
            (path / "driver.c").write_text(FIXTURE_DRIVER)
            objects = []
            game = [
                "-O2",
                "-finstrument-functions",
                "-fno-omit-frame-pointer",
                "-fno-optimize-sibling-calls",
                "-msse2",
                "-mfpmath=sse",
            ]
            for source, extra in [
                (path / "game.c", game),
                (ROOT / "trace.c", ["-O2"]),
                (ROOT / "test_charge_stub.c", ["-O2"]),
                (path / "table.c", []),
                (path / "driver.c", []),
            ]:
                obj = path / (source.stem + ".o")
                subprocess.run(
                    [
                        args.compiler,
                        "-m32",
                        "-Wall",
                        "-Werror",
                        *extra,
                        "-I" + str(ROOT),
                        "-c",
                        str(source),
                        "-o",
                        str(obj),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                objects.append(str(obj))
            exe = path / "fixture"
            subprocess.run(
                [args.compiler, "-m32", "-no-pie", *objects, "-lpthread", "-o", str(exe)],
                check=True,
                capture_output=True,
                text=True,
            )
            out = path / "ls"
            env = {
                "N64C_LOCKSTEP": "1",
                "N64C_LOCKSTEP_DIR": str(out),
                "N64C_LOCKSTEP_WATCH": "0x80001000",
                "PATH": "/usr/bin:/bin",
            }
            result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.split(),
                [
                    "add=5",
                    "half=1.5",
                    "wide=900000005",
                    "depth=43",
                    "after=2",
                    "inline=234",
                    "indirect=10",
                    "recursive_inline=102",
                ],
            )
            self.assertIn("boot-lockstep: on, 1024 pages", result.stderr)
            self.assertIn("boot-lockstep: 9 points written", result.stderr)
            polls = [json.loads(x) for x in (out / "lockstep.jsonl").read_text().splitlines() if '"k":"poll"' in x]
            # the oracle's poll line key for key, its own baseline: every page at poll 1, then those run() changed
            self.assertEqual([list(x) for x in polls], [["k", "poll", "vi", "pad", "pages"]] * 3)
            self.assertEqual(
                [(x["poll"], x["vi"], x["pad"]) for x in polls],
                [(1, 2, ["80000000", "00000000"]), (2, 4, ["1000b050", "040005fb"]), (3, 6, ["1000b050"])],
            )
            self.assertEqual(len(polls[0]["pages"]), 1024)
            self.assertTrue(polls[1]["pages"])
            self.assertEqual(polls[2]["pages"], [])
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["lockstep.jsonl"])  # hashes only
            self.assertNotIn("boot-shim-bind:", result.stderr)
            self.assertNotIn("boot-shim:", result.stderr)
            side = ls.Side(out, lambda r: r.get("order"))
            self.assertEqual(sorted(side.points), [1, 2, 3, 4, 5, 6, 7, 8, 9])
            self.assertEqual(side.meta["bytes"], "native")
            first = side.points[1][0]
            self.assertEqual(first[2], ls.page_hash(b"\x78\x56\x34\x12" + bytes(4092), native=True))
            self.assertEqual(side.points[3][0][1], ls.page_hash(b"\x05" + bytes(4095), native=True))  # add wrote 5
            # the dump replay: the same run to poll 2, nothing recorded, pages 1 and 2 written, the process ended there
            record = (out / "lockstep.jsonl").read_bytes()
            dumped = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=False,
                                    env={**env, "N64C_LOCKSTEP_DUMP": "2:2,1"})  # fmt: skip
            self.assertEqual(dumped.returncode, 0, dumped.stderr)
            self.assertIn("boot-lockstep: dump at poll 2", dumped.stderr)
            self.assertNotIn("points written", dumped.stderr)  # it ended at poll 2, before the summary
            self.assertEqual((out / "lockstep.jsonl").read_bytes(), record)
            self.assertEqual([p.name for p in (out / "dump").iterdir()], ["2"])
            self.assertEqual([p.name for p in (out / "dump" / "2").iterdir()], ["dump.bin"])
            rows = ls.read_dump(out / "dump" / "2" / "dump.bin")
            self.assertEqual([pg for pg, _, _ in rows], [1, 2])
            at2 = {}
            for p in polls[:2]:
                at2.update((pg, int(h, 16)) for pg, h in p["pages"])
            for pg, h, data in rows:
                self.assertEqual((ls.page_hash(data, native=True), at2[pg]), (h, h))  # the recorded state
            self.assertEqual(rows[0][2][:1], b"\x02")  # add(1, 1) stored last
            self.assertEqual(rows[1][2][:4], b"\x78\x56\x34\x12")
            side = ls.Side(out, lambda r: r.get("order"))
            self.assertEqual((sorted(side.dumps[2]), side.bad_dumps), ([1, 2], []))
            self.assertEqual(
                [side.calls[2][i]["a"][:2] for i in range(3)],
                [["00000002", "00000003"], ["00000000", "00000028"], ["00000001", "00000001"]],
            )
            self.assertEqual([side.rets[2][i]["r"][0] for i in range(3)], ["00000005", "00000028", "00000002"])
            self.assertEqual(side.rets[3][0]["st0"], f"{struct.unpack('<Q', struct.pack('<d', 1.5))[0]:016x}")
            self.assertEqual(side.rets[4][0]["r"], ["00000005", "00000009"])
            self.assertEqual(
                [side.rets[5][i]["r"][0] for i in range(4)], ["0000002b", "0000002a", "00000029", "00000028"]
            )
            self.assertNotIn(6, side.rets)  # longjmp left every leave frame without a return
            # Twelve inlined entries have no bump frame. They must not consume its
            # call budget or hijack inline_parent's return with fabricated samples.
            records = [json.loads(line) for line in (out / "lockstep.jsonl").read_text().splitlines()]
            skipped = [r for r in records if r["k"] == "unframed"]
            self.assertEqual([r["order"] for r in skipped], [7, 9])
            self.assertEqual(list(side.calls[7]), [0])
            self.assertEqual(side.calls[7][0]["a"][0], "00000007")
            self.assertEqual(side.rets[7][0]["r"][0], "0000000a")
            self.assertEqual([side.calls[8][i]["a"][0] for i in range(8)], [f"{i:08x}" for i in range(8)])
            self.assertEqual([side.rets[8][i]["r"][0] for i in range(8)], [f"{3 * i + 3:08x}" for i in range(8)])
            self.assertEqual([side.calls[9][i]["a"][0] for i in range(8)], [f"{i:08x}" for i in range(8)])
            self.assertEqual([side.rets[9][i]["r"][0] for i in range(8)], [f"{i + 3:08x}" for i in range(8)])
            self.assertEqual([w["new"] for w in side.watch], ["00000005", "00000028", "00000002"])
            # One run, both recorders: lockstep's poll and call lines as alone, and beside them calls.jsonl's
            # whole-movie call/ret lines keyed by (fn, caller, o) with the poll they ran in, which
            # `lockstep.py match` reads.
            both = path / "both"
            env = {"N64C_LOCKSTEP": "1", "N64C_LOCKSTEP_DIR": str(both), "N64C_CALLS": str(both)}
            env["PATH"] = "/usr/bin:/bin"
            result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("boot-lockstep: on", result.stderr)
            self.assertIn("boot-calls: on, first 16 calls", result.stderr)
            lines = [json.loads(x) for x in (both / "lockstep.jsonl").read_text().splitlines()]
            self.assertEqual([(x["poll"], x["vi"], x["pad"]) for x in lines if x["k"] == "poll"],
                             [(x["poll"], x["vi"], x["pad"]) for x in polls])  # fmt: skip
            alone = [json.loads(x) for x in (out / "lockstep.jsonl").read_text().splitlines()]
            self.assertEqual([(x["k"], x["order"], x["i"]) for x in lines if x["k"] in ("call", "ret")],
                             [(x["k"], x["order"], x["i"]) for x in alone if x["k"] in ("call", "ret")])  # fmt: skip
            self.assertFalse([x for x in lines if "o" in x])
            calls = [json.loads(x) for x in (both / "calls.jsonl").read_text().splitlines()]
            self.assertEqual((calls[0]["k"], calls[0]["mode"]), ("meta", "calls"))
            made = [x for x in calls if x["k"] == "call"]
            self.assertGreater(len(made), 8)  # past lockstep's 8 calls of a function: calls keeps its own first 16
            self.assertTrue(all({"fn", "caller", "o", "frame"} <= set(x) for x in made))
            self.assertEqual({x["frame"] for x in made}, {1})  # run() is between poll 1 and poll 2
            self.assertTrue(all({"fn", "caller", "o", "m"} <= set(x) for x in calls if x["k"] == "ret"))
            movie = ls.Side(both, lambda r: None)
            self.assertEqual(sorted(movie.polls), [1, 2, 3])
            self.assertEqual(set(movie.movie_calls), {(x["fn"], x["i"]) for x in made})  # keyed by (fn, call index)
            self.assertEqual(movie.keep_limit(), 16)  # the calls meta's `first`
            self.assertEqual({r["poll"] for r in movie.movie_calls.values()}, {1})  # the port's stamp is its poll
            self.assertTrue(movie.movie_rets and set(movie.movie_rets) <= set(movie.movie_calls))
            plain = subprocess.run(
                [str(exe)],
                capture_output=True,
                text=True,
                timeout=30,
                env={"PATH": "/usr/bin:/bin"},
                cwd=path,
                check=False,
            )
            self.assertEqual(plain.returncode, 0)
            self.assertNotIn("boot-lockstep", plain.stderr)
            self.assertFalse((path / "lockstep").exists())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
