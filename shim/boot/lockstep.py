# SPDX-License-Identifier: GPL-3.0-or-later
"""Lockstep divergence: the first point where the port's boot behaves differently from the oracle's.

Both sides record the same thing at the first hit of each boot-trace function (port: trace.c with N64C_LOCKSTEP=1;
oracle: cake-taste hosts/n64recomp lockstep.cpp with N64R_LOCKSTEP_DIR): per-4-KiB-page hashes of RDRAM (hashes only,
no page bytes), and the arguments and return of the first 8 calls of each function. Page bytes come from a dump: both
sides replay the movie to one poll (N64C_LOCKSTEP_DUMP / N64R_LOCKSTEP_DUMP = POLL:PAGE,...) and write just those pages
to <side>/dump/POLL/dump.bin; every dump a side holds gives the bytes of the hashes it names (Side.bytes).

    lockstep.py types --build BUILD --trace boot-trace.tsv --out types.tsv
    lockstep.py match --oracle DIR --port DIR [--symbols symbol_addrs.txt] [--port-exe EXE] [--persist N] [--types T]
                      [--json out]
    lockstep.py match --run RUN [--symbols symbol_addrs.txt] [--port-exe EXE] [--persist N]
    lockstep.py check RUN
    lockstep.py coverage --run RUN
    lockstep.py pages --run RUN [--poll P]
    lockstep.py compare --oracle DIR --port DIR --trace boot-trace.tsv [--symbols symbol_addrs.txt]
                        [--port-exe rage_decomp_boot] [--types types.tsv] [--ignore ignore.tsv] [--oracle2 DIR]
                        [--rom baserom.z64] [--json out.json]

compare lists every persistent memory, argument, and return divergence, in order within each category.
The reviewed --accept file marks exact known differences without hiding them; first_after_accepted and
matched_through_after_accepted describe the remaining differences. Raw first/matched_through remain unchanged.
compare prints the first diverging (function, page, symbol+offset, words) and the matched-through number: the largest
first-hit order up to which neither memory nor any recorded call diverges persistently. The oracle keeps canonical
big-endian bytes and the port native ones, so a word matches when the port's bytes are the oracle's as bytes, as
16-bit halves, as one 32-bit value, as half of a 64-bit value, or as the same function or data object (native address
vs original: the port keeps game statics in its executable, so a pointer to native D_80123456+8 is 0x8012345e).
Words that already differ at the first trace function (loader vs shim setup) count only once the game writes them.
Typed call fields that differ between both references are listed as volatile_calls, not persistent differences.
Calls of functions without a known prototype (--types) are not compared: their arity is unknown. Guest stack pages
(from the oracle's stack pointers), words that differ between two oracle runs (--oracle2), ignore.tsv rows (SYMBOL or
0xSTART-0xEND, then a reason) and, with --rom, oracle words still holding IPL3's boot copy of the ROM (the port runs
that code natively and never copies it) are masked. A memory divergence is persistent when the word
still differs at the next --persist compared points. Words holding a host pointer (outside RDRAM, not a function)
are shim-owned state, and floats within 4 ulp are rounding (MIPS single vs x87): listed, never counted. Exit 0 when nothing diverges, 1 when something does, 2 on bad input.

match takes a whole TAS movie played on both sides (`poll` lines: poll, vi, pad, pages since the previous poll; the
port's and the oracle's are the same line when the poll matches) and lists per class, in poll order, the persistent
divergences: input (pad words or a poll one side lacks), vi (the poll where the VI offset changes), calls (whole-movie
call/ret lines keyed by fn and its call index i, which neither side's caller attribution changes: a function one side never
calls, a call in another poll, and, for a function --types gives a prototype, other args or return) and memory (a word that differs at a
poll and the next --persist polls); matched_through_poll is the last poll before the first divergence of any class.
match --run RUN reads the run record (CONTRACTS.md "Run record"): RUN/lockstep/{oracle,port}/ in, RUN/lockstep/match.json
out. check RUN validates a run record field by field against that contract; exit 0 when it holds, 1 when a field breaks it.
coverage --run RUN joins reach.bin (over its run.json census), lockstep/port/calls.jsonl and match.json into
RUN/coverage.tsv, one row per function; exit 2 when an input is missing.
A page whose two hashes differ is compared word by word only when both sides hold its bytes (a dump); otherwise match
lists the page itself (memory class `page`, both hashes) and its report's `dump` names the poll and the pages a dump
replay must write (`decomp tas lockstep-dump RUN`); compare lists such a page the same way. pages --run RUN prints that
request ({poll, pages}, or null), from match.json or, with --poll, for that poll.
"""  # noqa: E501

import argparse
import array
import bisect
import concurrent.futures
import csv
import hashlib
import importlib.util
import itertools
import json
import os
import pathlib
import re
import struct
import subprocess
import sys

import numpy as np

# The sibling portdir.py is loaded by its path: importing lockstep (a test exec-loading it) never touches sys.path,
# which would shadow tests/ modules with shim/boot ones of the same name for the rest of the process.
_PORTDIR = importlib.util.spec_from_file_location("portdir", pathlib.Path(__file__).resolve().parent / "portdir.py")
portdir = importlib.util.module_from_spec(_PORTDIR)
_PORTDIR.loader.exec_module(portdir)

sys.dont_write_bytecode = True
PAGE = 4096
WORDS = PAGE // 4
RDRAM = 0x80000000
EMPTY_WORDS = (np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, object))
FNV_OFFSET, FNV_PRIME, MASK64 = 0xCBF29CE484222325, 0x100000001B3, (1 << 64) - 1
ORACLE_STACK, PORT_STACK = 0x10000, 0x100000
DUMP, DUMP_FILE, DUMP_HEAD = "dump", "dump.bin", 16  # <side>/dump/<poll>/dump.bin: <u32 page><u32 0><u64 hash> + page


def page_hash(data, native=False):
    """FNV-1a over little-endian 64-bit words of the canonical big-endian page, as trace.c and lockstep.cpp compute it:
    a native page (the port's, host-order 32-bit words) is byte-swapped word by word first."""
    if native:
        words = array.array("I", data)
        words.byteswap()
        data = words.tobytes()
    h = FNV_OFFSET
    for (w,) in struct.iter_unpack("<Q", data):
        h = ((h ^ w) * FNV_PRIME) & MASK64
    return h


ZERO_PAGE = page_hash(bytes(PAGE))


# ---- inputs -----------------------------------------------------------------------------------------------------
def read_trace(path):
    """order -> names (oracle symbol first) and name -> order, from boot-trace.tsv."""
    names, order_of = {}, {}
    for number, line in enumerate(pathlib.Path(path).read_text().splitlines()):
        fields = line.split("\t")
        if number == 0 or len(fields) < 3 or not fields[0].isdigit():
            continue
        order = int(fields[0])
        names[order] = [n for n in fields[2:] if n]
        for n in names[order]:
            order_of.setdefault(n, order)
    if not names:
        raise ValueError(f"{path}: empty trace")
    return names, order_of


def read_symbols(path):
    """Sorted (address, name) from symbol_addrs.txt, and name -> address."""
    rows, by_name = [], {}
    if path:
        for line in pathlib.Path(path).read_text().splitlines():
            m = re.match(r"\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;", line)
            if m:
                rows.append((int(m[2], 16), m[1]))
                by_name.setdefault(m[1], int(m[2], 16))
    rows.sort()
    return rows, by_name


def read_exe_symbols(path, kinds):
    """Sorted (address, size, name, local) of the port executable's symbols of the nm kinds given."""
    if not path:
        return []
    out = subprocess.run(["nm", "-S", "-n", "--defined-only", str(path)], capture_output=True, text=True, check=True)
    rows = []
    for line in out.stdout.splitlines():
        f = line.split()
        if len(f) == 4 and f[2] in kinds:
            rows.append((int(f[0], 16), int(f[1], 16), f[3], f[2].islower()))
        elif len(f) == 3 and f[1] in kinds and f[1] in "Tt":
            # Assembly/linker function aliases may have no ELF size. Their
            # exact entry address is still authoritative code identity.
            rows.append((int(f[0], 16), 0, f[2], f[1].islower()))
    return rows


def read_exe_functions(path):
    return read_exe_symbols(path, "Tt")


def read_exe_data(path):
    return [r for r in read_exe_symbols(path, "DdBbRr") if r[1]]


BOOT_COPY = (0x80000400, 0x1000, 0x100000)  # IPL3 copies ROM 0x1000.. (1 MiB) to 0x80000400


def read_boot_copy(path):
    """IPL3's boot copy as canonical big-endian bytes, from a z64/v64/n64 ROM, or None."""
    if not path:
        return None
    rom = bytearray(pathlib.Path(path).read_bytes()[: BOOT_COPY[1] + BOOT_COPY[2]])
    magic = bytes(rom[:4])
    if magic == bytes.fromhex("37804012"):
        rom[0::2], rom[1::2] = rom[1::2], rom[0::2]
    elif magic == bytes.fromhex("40123780"):
        words = array.array("I", bytes(rom[: len(rom) // 4 * 4]))
        words.byteswap()
        rom = bytearray(words.tobytes())
    elif magic != bytes.fromhex("80371240"):
        raise ValueError(f"{path}: not an N64 ROM")
    return bytes(rom[BOOT_COPY[1] :])


def read_dump(path):
    """[(page, hash, bytes)] of one dump.bin, in file order; a record cut short is refused."""
    data = pathlib.Path(path).read_bytes()
    step = DUMP_HEAD + PAGE
    if len(data) % step:
        raise ValueError(f"{path}: {len(data)} bytes is not whole {step}-byte records")
    out = []
    for at in range(0, len(data), step):
        page, _, h = struct.unpack_from("<IIQ", data, at)
        out.append((page, h, data[at + DUMP_HEAD : at + step]))
    return out


class Side:
    """One run's lockstep files: points (key -> page-hash array), calls and returns (key -> index -> record), polls
    (index -> record without its pages: poll_pages streams them) and the page bytes its dumps hold (dumps: poll -> page
    -> hash; bytes(hash))."""

    def __init__(self, directory, key_of):
        self.dir = pathlib.Path(directory)
        self.points, self.calls, self.rets, self.sps, self.watch = {}, {}, {}, {}, []
        self.polls, self.movie_calls, self.movie_rets = {}, {}, {}
        self.meta, self.calls_first = None, None
        with open(self.dir / "lockstep.jsonl") as stream:
            lines = itertools.chain(stream, self.movie_lines(self.dir / "calls.jsonl"))
            self.read_lines(lines, key_of)
        if self.meta is None:
            raise ValueError(f"{self.dir}: no meta line")
        self.canonical = self.meta.get("bytes") == "canonical-be"
        self.read_dumps()

    def read_lines(self, lines, key_of):
        # A whole-movie call's poll: its stamp when the side declares the stamp a poll index (the port's calls.jsonl
        # `frame` is its poll; an oracle meta `"frame":"poll"`), else the poll line before it in this stream (an older
        # oracle stamped the retrace count, never a poll).
        current, poll, stamped = None, 0, True
        for number, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as err:
                if number:  # the last line of a run killed mid-write
                    continue
                raise ValueError(f"{self.dir}: unreadable meta line") from err
            kind = r.get("k")
            if kind == "meta":
                self.meta = r
                current = array.array("Q", [0]) * (r["ram"] // PAGE)
                stamped = r.get("bytes") != "canonical-be" or r.get("frame") == "poll"
                continue
            if current is None:
                raise ValueError(f"{self.dir}: no meta line")
            if "sp" in r:
                self.sps.setdefault(r.get("thr", 0), []).append(int(r["sp"], 16))
            if kind == "hit":
                for page, h in r["pages"]:
                    current[page] = int(h, 16)
                key = key_of(r)
                if key is not None and key not in self.points:
                    self.points[key] = (array.array("Q", current), r)
            elif kind == "poll":
                r.pop("pages", None)
                self.polls[r["poll"]] = r
                poll = r["poll"]
            elif kind in ("call", "ret") and "o" in r:  # a whole-movie call, keyed by (fn, the function's call index)
                if kind == "call":
                    r["poll"] = r.get("frame", 0) if stamped else poll
                movie = self.movie_calls if kind == "call" else self.movie_rets
                movie[(r.get("fn"), r.get("i"))] = r  # check_side names a line without them
            elif kind in ("call", "ret"):
                key = key_of(r)
                if key is not None:
                    (self.calls if kind == "call" else self.rets).setdefault(key, {})[r["i"]] = r
                elif kind == "ret" and "fn" in r and "i" in r:  # the oracle's ret has no o: its call's (fn, i) keys it
                    self.movie_rets[(r["fn"], r["i"])] = r
            elif kind == "watch":
                self.watch.append(r)

    def read_dumps(self):
        # The record is hashes only. Page bytes are the dumps' (<side>/dump/<poll>/dump.bin, a few pages each), keyed by
        # hash: a dump record whose bytes do not hash to its key is no evidence (bad_dumps) and gives no bytes.
        self.dumps, self.bad_dumps = {}, []
        self.dumped = {page_hash(bytes(PAGE)): bytes(PAGE)}
        folders = (self.dir / DUMP).iterdir() if (self.dir / DUMP).is_dir() else ()
        for folder in sorted(f for f in folders if f.name.isdigit() and (f / DUMP_FILE).is_file()):
            poll = int(folder.name)
            for page, h, data in read_dump(folder / DUMP_FILE):
                self.dumps.setdefault(poll, {})[page] = h
                if page_hash(data, native=not self.canonical) == h:
                    self.dumped[h] = data
                else:
                    self.bad_dumps.append((poll, page, h))

    def poll_pages(self):
        """Each poll line's (poll, [[page, hash hex], ...]) in file order, read again from lockstep.jsonl."""
        with open(self.dir / "lockstep.jsonl") as stream:
            for line in stream:
                if '"poll"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue  # the last line of a run killed mid-write
                if r.get("k") == "poll":
                    yield r["poll"], r.get("pages", [])

    def movie_lines(self, path):
        """The whole-movie call and ret lines (keyed by fn, i) of the calls.jsonl a port run with both N64C_LOCKSTEP
        and N64C_CALLS writes beside lockstep.jsonl; none when the run had no N64C_CALLS. Its meta gives the calls kept
        per (caller, function) (calls_first) and is not the lockstep stream's; unframed lines are not either, and a
        line cut by a killed run is dropped."""
        if not path.exists():
            return []
        kept = []
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("k") == "meta":
                self.calls_first = r.get("first")
            elif r.get("k") in ("call", "ret") and "o" in r:
                kept.append(line)
        return kept

    def keep_limit(self):
        """Calls kept per (caller, function) before the power-of-two ring: the port's calls.jsonl `first`, the
        oracle's meta `calls`."""
        return self.calls_first if self.calls_first is not None else self.meta.get("calls")

    def bytes(self, h):
        """The page bytes of hash h (the zero page, or a dump's), None when no dump holds them."""
        return self.dumped.get(h)


# ---- word equivalence -------------------------------------------------------------------------------------------
def near_float(ov, pv, ulps=4):
    """Both words are the same ordinary-magnitude float but for the last bits (MIPS vs x87/SSE rounding)."""
    if (ov ^ pv) & 0x80000000 or abs(ov - pv) > ulps:
        return False
    exponent = (ov >> 23) & 0xFF
    return 0x60 <= exponent <= 0x9F


class Matcher:
    def __init__(self, ram, symbols, exe_functions, exe_data=()):
        self.ram = ram
        self.sym_rows, self.sym_addr = symbols
        self.sym_keys = [a for a, _ in self.sym_rows]
        self.exe = exe_functions
        self.exe_keys = [a for a, *_ in exe_functions]
        self.original_functions = set()
        for _, _, name, _ in exe_functions:
            if name in self.sym_addr:
                self.original_functions.add(self.sym_addr[name])
        self.data = []
        for address, size, name, _ in sorted(exe_data):
            original = self.original_address(name)
            if original is not None:
                self.data.append((address, size, original))
        self.data_keys = [a for a, _, _ in self.data]
        self.stacks = ([], [])
        # page_words' vector forms of port_function, port_data and the original function set
        self.function_values = np.array(sorted(self.original_functions), dtype=np.int64)
        self.exe_columns = np.array([(a, n) for a, n, *_ in self.exe], dtype=np.int64).reshape(-1, 2).T
        self.data_columns = np.array(self.data, dtype=np.int64).reshape(-1, 3).T

    def set_stacks(self, oracle_sps, port_sps):
        """Every thread's stack span on each side, so a pointer to any thread's locals matches the other's."""

        def spans(sps, below, above):
            return [(min(v) - below, max(v) + above) for v in sps.values() if v and max(v) - min(v) < 0x100000]

        self.stacks = (spans(oracle_sps, PAGE, ORACLE_STACK), spans(port_sps, PAGE, PORT_STACK))

    def both_stack(self, oracle_value, port_value):
        return any(lo <= oracle_value < hi for lo, hi in self.stacks[0]) and any(
            lo <= port_value < hi for lo, hi in self.stacks[1]
        )

    def original_address(self, name):
        """The original address of a native data object: its symbol_addrs entry or its D_XXXXXXXX name."""
        name = name.split(".")[0]
        if name in self.sym_addr:
            return self.sym_addr[name]
        m = re.fullmatch(r"[A-Za-z]+_(8[0-9A-Fa-f]{7})", name)
        return int(m[1], 16) if m else None

    def port_data(self, value):
        """The original address of native address value inside a game data object (one past its end too), or None."""
        i = bisect.bisect_right(self.data_keys, value) - 1
        if i < 0:
            return None
        address, size, original = self.data[i]
        return original + value - address if value <= address + size else None

    def same_address(self, oracle_value, port_value):
        """The port value is the oracle's code or data address in native form."""
        return self.same_function(oracle_value, port_value) or (
            RDRAM <= oracle_value < RDRAM + self.ram and self.port_data(port_value) == oracle_value
        )

    def port_function(self, value):
        """The original address of the port function at native address value, or None."""
        i = bisect.bisect_right(self.exe_keys, value) - 1
        if i < 0:
            return None
        address, size, name, _ = self.exe[i]
        if value != address and not (size and value < address + size):
            return None
        return self.sym_addr.get(name, -1)

    def is_local(self, names):
        if not hasattr(self, "_local"):
            self._local = {name: local for _, _, name, local in self.exe}
        return any(self._local.get(n, False) for n in names)

    def same_function(self, oracle_value, port_value):
        if oracle_value not in self.original_functions:
            return False
        i = bisect.bisect_right(self.exe_keys, port_value) - 1
        if i < 0:
            return False
        address, size, _, _ = self.exe[i]
        if port_value != address and not (size and port_value < address + size):
            return False
        # Linker aliases share one native entry but may have distinct guest
        # addresses (for example a mapped callback label). Preserve every
        # symbol-proven identity instead of selecting the last equal-address name.
        while i >= 0 and self.exe[i][0] == address:
            if self.sym_addr.get(self.exe[i][2]) == oracle_value:
                return True
            i -= 1
        return False

    def host_pointer(self, oracle_value, port_value):
        in_ram = RDRAM <= port_value < RDRAM + self.ram
        return (
            not in_ram
            and port_value > 0xFFFF
            and self.port_function(port_value) is None
            and self.port_data(port_value) is None
            and (oracle_value == 0 or RDRAM <= oracle_value < RDRAM + self.ram)
        )

    def symbol(self, address):
        i = bisect.bisect_right(self.sym_keys, address) - 1
        if i < 0:
            return f"0x{address:08x}"
        base, name = self.sym_rows[i]
        return f"{name}+0x{address - base:x}" if address != base else name

    def page_diff(self, oracle, port):
        """Word indices of the page that differ, each with its class (value, fp-ulp or host-pointer)."""
        ws, _, _, kinds = self.page_words(oracle, port)
        return dict(zip(ws.tolist(), kinds.tolist(), strict=True))

    def page_words(self, oracle, port):
        """The differing words of a page pair as arrays (word index, oracle value, port value, class). A whole movie
        diffs millions of page pairs (pp-1p-match: 3.4M differing words in its first 800 polls, 1.35M of them pointer
        candidates), so every test of page_diff's word rules runs over the whole page at once."""
        if oracle == port:
            return EMPTY_WORDS
        raw, got = np.frombuffer(oracle, "<u4"), np.frombuffer(port, "<u4")
        same = got == raw  # the same bytes, byte-reversed, or with either or both halfwords swapped
        same |= got == raw.byteswap()
        same |= got == ((raw & 0xFF) << 8 | (raw >> 8) & 0xFF | raw & 0xFFFF0000)
        same |= got == ((raw & 0xFFFF) | (raw >> 8) & 0xFF0000 | (raw << 8) & 0xFF000000)
        same |= got == ((raw & 0x00FF00FF) << 8 | (raw >> 8) & 0x00FF00FF)
        same |= (np.frombuffer(port, "<u8") == np.frombuffer(oracle, ">u8")).repeat(2)  # a double stored reversed
        ws = np.flatnonzero(~same)
        if not ws.size:
            return EMPTY_WORDS
        ov = np.frombuffer(oracle, ">u4")[ws].astype(np.int64)
        pv = got[ws].astype(np.int64)
        in_ram = lambda v: (v >= RDRAM) & (v < RDRAM + self.ram)  # noqa: E731
        function = np.zeros(ws.size, bool)  # port_function(pv) is not None
        if self.exe_columns.size:
            i = np.searchsorted(self.exe_columns[0], pv, "right") - 1
            address, size = self.exe_columns[0][i], self.exe_columns[1][i]
            function = (i >= 0) & ((pv == address) | ((size > 0) & (pv < address + size)))
        data, original = np.zeros(ws.size, bool), np.full(ws.size, -1, np.int64)  # port_data(pv) is not None, its value
        if self.data_columns.size:
            i = np.searchsorted(self.data_columns[0], pv, "right") - 1
            address = self.data_columns[0][i]
            data = (i >= 0) & (pv <= address + self.data_columns[1][i])
            original = np.where(data, self.data_columns[2][i] + pv - address, -1)
        keep = ~(in_ram(ov) & (original == ov))  # same_address by data; by function below, per word
        for k in np.flatnonzero(keep & np.isin(ov, self.function_values)).tolist():
            keep[k] = not self.same_function(int(ov[k]), int(pv[k]))
        host = ~in_ram(pv) & (pv > 0xFFFF) & ~function & ~data & ((ov == 0) | in_ram(ov))
        near = (((ov ^ pv) & 0x80000000) == 0) & (np.abs(ov - pv) <= 4) & ((ov >> 23 & 0xFF) >= 0x60)
        near &= (ov >> 23 & 0xFF) <= 0x9F
        kinds = np.where(host, "host-pointer", np.where(near, "fp-ulp", "value")).astype(object)
        return ws[keep], ov[keep], pv[keep], kinds[keep]


# ---- types (gcc -aux-info over the boot units) --------------------------------------------------------------------
BUILTIN = {
    "void": "void",
    "float": "float",
    "double": "double",
    "long double": "agg",
    "char": "int8",
    "signed char": "int8",
    "unsigned char": "uint8",
    "short": "int16",
    "short int": "int16",
    "signed short": "int16",
    "unsigned short": "uint16",
    "unsigned short int": "uint16",
    "int": "int32",
    "signed": "int32",
    "signed int": "int32",
    "unsigned": "int32",
    "unsigned int": "int32",
    "long": "int32",
    "long int": "int32",
    "unsigned long": "int32",
    "long unsigned int": "int32",
    "unsigned long int": "int32",
    "long long": "int64",
    "long long int": "int64",
    "unsigned long long": "int64",
    "long long unsigned int": "int64",
    "unsigned long long int": "int64",
    "_Bool": "uint8",
}
QUALIFIERS = {"const", "volatile", "register", "extern", "static", "inline", "__inline__", "__restrict", "restrict"}
DECL = re.compile(r"/\* (.*?):(\d+):([NO])([CF]) \*/ (.*?);")
TYPEDEF = re.compile(r"\btypedef\s+([^;{}()]+?)\s+(\w+)\s*(\[[^\]]*\])?\s*;")
AGG_TYPEDEF = re.compile(r"\btypedef\s+(?:struct|union)\b[^;]*?\}\s*(\w+)\s*;", re.DOTALL)


def kind_of(text, typedefs, depth=0):
    words = [w for w in text.replace("*", " * ").split() if w not in QUALIFIERS]
    if "*" in words or "[" in text or "(" in text:
        return "ptr"
    base = " ".join(words)
    if base in BUILTIN:
        return BUILTIN[base]
    if words and words[0] == "enum":
        return "int32"
    if words and words[0] in ("struct", "union"):
        return "agg"
    if base in typedefs and depth < 16:
        return kind_of(typedefs[base], typedefs, depth + 1)
    return "unknown"


def split_params(text):
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
            continue
        depth += ch in "(["
        depth -= ch in ")]"
        cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def param_type(text, typedefs):
    """A parameter declaration without its name."""
    if "(" in text:
        return "ptr"
    words = text.replace("*", " * ").split()
    if (
        len(words) >= 2
        and words[-1] != "*"
        and re.fullmatch(r"\w+", words[-1])
        and words[-1] not in BUILTIN
        and not any(words[-1] in k.split() for k in BUILTIN)
        and words[-2] not in ("struct", "union", "enum")
    ):
        words = words[:-1]
    return kind_of(" ".join(words), typedefs)


def parse_aux(text, wanted, typedefs, out):
    for m in DECL.finditer(text):
        decl = m[5]
        lp = decl.find("(")
        if lp < 0:
            continue
        head, params = decl[:lp].strip(), decl[lp + 1 : decl.rfind(")")]
        name = head.split()[-1].lstrip("*") if head.split() else ""
        if name not in wanted or (name in out and m[4] == "C"):
            continue
        ret = kind_of(head[: len(head) - len(name)].strip(), typedefs)
        if m[3] == "O" or not params.strip():
            kinds = None if m[3] == "O" else []
        elif params.strip() == "void":
            kinds = []
        else:
            kinds = []
            for p in split_params(params):
                if p == "...":
                    kinds.append("var")
                    break
                kinds.append(param_type(p, typedefs))
        out[name] = (ret, kinds)


def build_types(build, trace, out_path, compiler, jobs):
    names, _ = read_trace(trace)
    wanted = {n for row in names.values() for n in row}
    linked = pathlib.Path(build) / "decomp-boot" / "linked-objects.txt"
    if linked.exists():
        units = [pathlib.Path(p[:-2] + ".i") for p in linked.read_text().split() if p.endswith(".o")]
    else:
        units = sorted((pathlib.Path(build) / "decomp").rglob("*.i"))
    units = [u for u in units if u.exists()]
    work = pathlib.Path(out_path).with_suffix(".aux")
    work.mkdir(parents=True, exist_ok=True)

    def one(index_unit):
        index, unit = index_unit
        target = work / f"{index}.txt"
        # Match the native decompilation build's language contract. GCC's newer
        # default rejects conversions accepted by that build, dropping its types.
        target.unlink(missing_ok=True)
        subprocess.run(
            [
                compiler,
                "-m32",
                "-std=gnu89",
                "-fsyntax-only",
                "-w",
                "-aux-info",
                str(target),
                "-x",
                "cpp-output",
                str(unit),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return unit, target

    typedefs, found = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(one, enumerate(units)))
    for unit, _ in results[:50]:
        text = unit.read_text(errors="replace")
        for m in TYPEDEF.finditer(text):
            typedefs.setdefault(m[2], "void *" if m[3] else m[1])
        for m in AGG_TYPEDEF.finditer(text):
            typedefs.setdefault(m[1], "struct")
    for _, target in results:
        if target.exists():
            parse_aux(target.read_text(errors="replace"), wanted, typedefs, found)
    rows = ["name\treturn\tparams"]
    for name in sorted(found):
        ret, kinds = found[name]
        rows.append(f"{name}\t{ret}\t{'?' if kinds is None else ','.join(kinds)}")
    pathlib.Path(out_path).write_text("\n".join(rows) + "\n")
    return len(found), len(units)


def read_types(path):
    types = {}
    if path:
        for line in pathlib.Path(path).read_text().splitlines()[1:]:
            f = line.split("\t")
            if len(f) == 3:
                types[f[0]] = (f[1], None if f[2] == "?" else [k for k in f[2].split(",") if k])
    return types


# ---- calls ----------------------------------------------------------------------------------------------------------
def o32_slots(kinds):
    """Where each MIPS o32 argument lives: ('a', word) in a0..a3, ('f', 12|14), or None past the registers; and the
    byte offset of each in the port's i386 stack area."""
    where, port_at, word, fp_ok, fp_next, offset = [], [], 0, True, 12, 0
    for k in kinds:
        size = 8 if k in ("double", "int64") else 4
        if size == 8 and word % 2:
            word += 1
        if k in ("float", "double") and fp_ok and fp_next <= 14:
            where.append(("f", fp_next))
            fp_next += 2
        else:
            fp_ok = False
            where.append(("a", word) if word + size // 4 <= 4 else None)
        word += size // 4
        port_at.append(offset)
        offset += size
    return where, port_at


MASKS = {"int8": 0xFF, "uint8": 0xFF, "int16": 0xFFFF, "uint16": 0xFFFF}


def same_word(matcher, kind, ov, pv, osp, psp):
    m = MASKS.get(kind, 0xFFFFFFFF)
    if (ov & m) == (pv & m):
        return True
    if kind in ("ptr", "int32", "unknown"):
        if (abs(ov - osp) < ORACLE_STACK and abs(pv - psp) < PORT_STACK) or matcher.both_stack(ov, pv):
            return True
        return matcher.same_address(ov, pv)
    return False


def oracle_fields(kinds, ret_kind, call, returned):
    """Typed values in the reference ABI; never inspect unused registers."""
    fields = {}
    if call is not None and kinds is not None:
        where, _ = o32_slots(kinds)
        words = [int(x, 16) for x in call["a"]]
        for index, (kind, at) in enumerate(zip(kinds, where, strict=False)):
            if kind in ("var", "agg") or at is None:
                break
            if at[0] == "f":
                value = int(call[f"f{at[1]}"], 16)
                if kind == "float":
                    value &= 0xFFFFFFFF
            elif kind in ("double", "int64"):
                value = words[at[1]] << 32 | words[at[1] + 1]
            else:
                value = words[at[1]] & MASKS.get(kind, 0xFFFFFFFF)
            fields[f"arg{index}"] = (kind, value)
    if returned is not None and ret_kind not in (None, "void", "agg", "unknown"):
        if ret_kind in ("float", "double"):
            value = int(returned["f0"], 16)
            if ret_kind == "float":
                value &= 0xFFFFFFFF
        elif ret_kind == "int64":
            value = int(returned["r"][0], 16) << 32 | int(returned["r"][1], 16)
        else:
            value = int(returned["r"][0], 16) & MASKS.get(ret_kind, 0xFFFFFFFF)
        fields["return"] = (ret_kind, value)
    return fields


def reference_call_changes(kinds, ret_kind, call, peer, returned=None, peer_returned=None):
    """Only fields observed and unequal in BOTH references are nondeterministic.

    Missing samples prove nothing. Stack-address differences alone do not make a
    field volatile, consistent with the call comparator's stack equivalence.
    """
    if call is None or peer is None:
        return {}
    first = oracle_fields(kinds, ret_kind, call, returned)
    second = oracle_fields(kinds, ret_kind, peer, peer_returned)
    sp, peer_sp = int(call["sp"], 16), int(peer["sp"], 16)
    changes = {}
    for field in first.keys() & second.keys():
        kind, a = first[field]
        _, b = second[field]
        if a == b:
            continue
        if kind in ("ptr", "int32", "unknown") and abs(a - sp) < ORACLE_STACK and abs(b - peer_sp) < ORACLE_STACK:
            continue
        changes[field] = {"field": field, "type": kind, "oracle": f"0x{a:x}", "oracle2": f"0x{b:x}"}
    return changes


def compare_call(matcher, kinds, local, oc, pc):
    """All differing arguments of one recorded call, in parameter order."""
    differences = []
    a = [int(x, 16) for x in oc["a"]]
    f = {12: int(oc["f12"], 16), 14: int(oc["f14"], 16)}
    words = [int(x, 16) for x in pc["a"]]
    osp, psp = int(oc["sp"], 16), int(pc["sp"], 16)
    if kinds is None:  # no prototype: the arity is unknown, so registers past it hold anything
        return differences
    where, port_at = o32_slots(kinds)
    for index, (k, at, pos) in enumerate(zip(kinds, where, port_at, strict=False)):
        if k in ("var", "agg") or at is None or pos + (8 if k in ("double", "int64") else 4) > 32:
            break
        lo = words[pos // 4]
        hi = words[pos // 4 + 1] if pos // 4 + 1 < len(words) else 0
        if at[0] == "f":
            ov = f[at[1]] & 0xFFFFFFFF if k == "float" else f[at[1]]
            pv = lo if k == "float" else lo | hi << 32
        elif k in ("double", "int64"):
            ov, pv = a[at[1]] << 32 | a[at[1] + 1], lo | hi << 32
        else:
            ov, pv = a[at[1]], lo
        if k in ("double", "int64", "float") and ov != pv:
            differences.append(f"arg{index} {k} 0x{ov:x} vs port 0x{pv:x}")
        if k not in ("double", "int64", "float") and not same_word(matcher, k, ov, pv, osp, psp):
            differences.append(f"arg{index} {k} 0x{ov:08x} vs port 0x{pv:08x}")
    return differences


def compare_return(matcher, ret_kind, oret, pret, osp=0, psp=-PORT_STACK * 4):
    """The return's divergence, or None; pointers into the caller's stack (osp/psp: the call's sp) match."""
    if ret_kind in (None, "void", "agg", "unknown"):
        return None
    v0, v1 = (int(x, 16) for x in oret["r"])
    eax, edx = (int(x, 16) for x in pret["r"])
    if ret_kind in ("float", "double"):
        if pret.get("st0") is None:
            return f"return {ret_kind}: port left no st(0)"
        st0 = int(pret["st0"], 16)
        f0 = int(oret["f0"], 16)
        if ret_kind == "float":
            ov = f0 & 0xFFFFFFFF
            pv = struct.unpack("<I", struct.pack("<f", struct.unpack("<d", st0.to_bytes(8, "little"))[0]))[0]
        else:
            ov, pv = f0, st0
        return None if ov == pv else f"return {ret_kind} 0x{ov:x} vs port 0x{pv:x}"
    if ret_kind == "int64":
        ov, pv = v0 << 32 | v1, edx << 32 | eax
        return None if ov == pv else f"return int64 0x{ov:016x} vs port 0x{pv:016x}"
    if same_word(matcher, ret_kind, v0, eax, osp, psp):
        return None
    return f"return {ret_kind} 0x{v0:08x} vs port 0x{eax:08x}"


# ---- compare --------------------------------------------------------------------------------------------------------
def masked_ranges(args, matcher, oracle):
    """Masked byte ranges with reasons: guest stacks, ignore rows."""
    ranges = []
    for thread, sps in oracle.sps.items():
        lo, hi = min(sps), max(sps)
        if RDRAM <= lo < RDRAM + matcher.ram and hi - lo < 0x100000:
            ranges.append(((lo - PAGE) & ~(PAGE - 1), (hi + PAGE) & ~(PAGE - 1), f"guest stack of thread {thread}"))
    if args.ignore:
        keys = matcher.sym_keys
        for line in pathlib.Path(args.ignore).read_text().splitlines():
            f = line.split("\t")
            if not f[0].strip() or f[0].startswith("#"):
                continue
            reason = f[1] if len(f) > 1 else "ignored"
            m = re.fullmatch(r"(0x[0-9A-Fa-f]+)-(0x[0-9A-Fa-f]+)", f[0].strip())
            if m:
                ranges.append((int(m[1], 16), int(m[2], 16), reason))
            elif f[0].strip() in matcher.sym_addr:
                start = matcher.sym_addr[f[0].strip()]
                i = bisect.bisect_right(keys, start)
                end = keys[i] if i < len(keys) else start + 4
                ranges.append((start, min(end, start + 0x10000), reason))
    return ranges


def boot_unmask(diff, pg, oracle_page, boot, seen):
    """The page's differing words without those the oracle still holds as IPL3 copied them."""
    base = RDRAM + pg * PAGE - BOOT_COPY[0]
    if base + PAGE <= 0 or base >= len(boot):
        return diff
    out = {}
    for w, cls in diff.items():
        at = base + 4 * w
        if at >= 0 and at + 4 <= len(boot) and oracle_page[4 * w : 4 * w + 4] == boot[at : at + 4]:
            seen.add(pg * WORDS + w)
        else:
            out[w] = cls
    return out


def words_text(data, w, big):
    out = []
    for k in range(w, min(w + 4, WORDS)):
        out.append(f"{int.from_bytes(data[4 * k : 4 * k + 4], 'big' if big else 'little'):08x}")
    return " ".join(out)


ACCEPT_FIELDS = ("kind", "order", "function", "site", "detail", "reason", "reviewed_by")


def read_acceptances(path):
    """Strict exact-value review records; no range, wildcard, or silent masking."""
    with pathlib.Path(path).open() as stream:
        reader = csv.DictReader((line for line in stream if line.strip() and not line.startswith("#")), delimiter="\t")
        if reader.fieldnames != list(ACCEPT_FIELDS):
            raise ValueError("accept file must have columns: " + "\t".join(ACCEPT_FIELDS))
        rows, seen = [], set()
        for row in reader:
            if set(row) != set(ACCEPT_FIELDS) or any(not v or not v.strip() for v in row.values()):
                raise ValueError("accept row needs exact fields, a reason, and a reviewer")
            row["order"] = int(row["order"])
            if row["order"] < 1 or row["kind"] not in ("memory", "call"):
                raise ValueError("invalid accept kind or order")
            if row["kind"] == "memory":
                if not re.fullmatch(r"0x[0-9a-f]{8}", row["site"]) or not re.fullmatch(
                    r"[0-9a-f]{8}/[0-9a-f]{8}", row["detail"]
                ):
                    raise ValueError("memory acceptance needs an address and exact oracle/port words")
            elif not row["site"].isdigit():
                raise ValueError("call acceptance needs a call index")
            key = tuple(row[k] for k in ACCEPT_FIELDS[:5])
            if key in seen:
                raise ValueError("duplicate accept row")
            seen.add(key)
            rows.append(row)
    return rows


def apply_acceptances(report, reviews, common):
    lookup = {tuple(r[k] for k in ACCEPT_FIELDS[:5]): r for r in reviews}
    remaining, accepted, observed = [], [], set()
    for kind, rows in (("memory", report["memory"]), ("call", report["calls"])):
        for row in rows:
            row.pop("acceptance", None)
            site = row["address"] if kind == "memory" else str(row["call"])
            detail = row["oracle"].split()[0] + "/" + row["port"].split()[0] if kind == "memory" else row["why"]
            key = (kind, row["order"], row["function"], site, detail)
            review = lookup.get(key)
            if review:
                row["acceptance"] = {k: review[k] for k in ("reason", "reviewed_by")}
                accepted.append(dict(row, kind=kind))
                observed.add(key)
            else:
                remaining.append(dict(row, kind=kind))
    # Call before memory on a tie matches the existing raw-first rule.
    remaining.sort(key=lambda r: (r["order"], r["kind"] != "call"))
    report["first_after_accepted"] = remaining[0] if remaining else None
    bad = remaining[0]["order"] if remaining else None
    report["matched_through_after_accepted"] = max((k for k in common if bad is None or k < bad), default=0)
    report["accepted"] = sorted(accepted, key=lambda r: (r["order"], r["kind"] != "call"))
    report["unmatched_accepts"] = [r for key, r in lookup.items() if key not in observed]


def acceptance_text(row):
    review = row.get("acceptance")
    return f" [accepted: {review['reason']}; reviewed by {review['reviewed_by']}]" if review else ""


def compare(args):
    reviews = read_acceptances(args.accept)
    names, order_of = read_trace(args.trace)
    oracle = Side(args.oracle, lambda r: order_of.get(r.get("fn")) if "fn" in r else None)
    port = Side(args.port, lambda r: r.get("order"))
    if not oracle.canonical or port.canonical:
        raise ValueError("--oracle must be the canonical-be side and --port the native one")
    ram = min(oracle.meta["ram"], port.meta["ram"])
    matcher = Matcher(ram, read_symbols(args.symbols), read_exe_functions(args.port_exe), read_exe_data(args.port_exe))
    matcher.set_stacks(oracle.sps, port.sps)
    boot = read_boot_copy(args.rom)
    types = read_types(args.types)
    ranges = masked_ranges(args, matcher, oracle)
    masked_words = set()
    for lo, hi, _ in ranges:
        masked_words.update(range((max(lo, RDRAM) - RDRAM) // 4, (min(hi, RDRAM + ram) - RDRAM) // 4))
    volatile = set()
    other = None
    if args.oracle2:
        other = Side(args.oracle2, lambda r: order_of.get(r.get("fn")) if "fn" in r else None)
        for key in sorted(set(oracle.points) & set(other.points)):
            a, b = oracle.points[key][0], other.points[key][0]
            for pg in range(ram // PAGE):
                if a[pg] != b[pg]:
                    x, y = oracle.bytes(a[pg]), other.bytes(b[pg])
                    if x is None or y is None:  # no dump holds the bytes: the words cannot be told apart
                        continue
                    volatile.update(
                        pg * WORDS + w for w in range(WORDS) if x[4 * w : 4 * w + 4] != y[4 * w : 4 * w + 4]
                    )
    common = sorted(set(oracle.points) & set(port.points))
    cache = {}
    per_point, boot_words = [], set()
    for key in common:
        a, b = oracle.points[key][0], port.points[key][0]
        diff = {}
        for pg in range(ram // PAGE):
            pair = (a[pg], b[pg])
            if pair not in cache:
                o, p = oracle.bytes(a[pg]), port.bytes(b[pg])
                if pair[0] == pair[1]:  # one canonical hash: the same words
                    cache[pair] = {}
                elif o is None or p is None:  # no dump holds the bytes: the page itself differs
                    cache[pair] = None
                else:
                    cache[pair] = matcher.page_diff(o, p)
                    if boot is not None and cache[pair]:
                        cache[pair] = boot_unmask(cache[pair], pg, o, boot, boot_words)
            if cache[pair] is None:
                if not all(pg * WORDS + w in masked_words for w in range(WORDS)):
                    diff[-1 - pg] = ("page", pair)  # a page key: -1 - page, ahead of every word
                continue
            for w, cls in cache[pair].items():
                index = pg * WORDS + w
                if index not in masked_words and index not in volatile:
                    diff[index] = (cls, pair)
        per_point.append(diff)
    memory, shim, transient, setup, ulp = [], set(), 0, {}, set()

    def oracle_word(index, pair):
        if index < 0:  # a page without bytes: its oracle hash
            return pair[0]
        w = index % WORDS
        return oracle.bytes(pair[0])[4 * w : 4 * w + 4]

    for j, key in enumerate(common):
        previous = per_point[j - 1] if j else {}
        later = per_point[j + 1 : j + 1 + args.persist]
        for index, (cls, pair) in sorted(per_point[j].items()):
            if j == 0 and key == min(names):
                # before the first game function: loader (IPL3) vs shim setup, not game behaviour; re-armed once
                # the game writes the word
                setup[index] = oracle_word(index, pair)
                continue
            if index in setup:
                if oracle_word(index, pair) == setup[index]:
                    continue
                del setup[index]
            elif index in previous:
                continue
            if cls == "host-pointer":
                shim.add(index)
                continue
            if cls == "fp-ulp":
                ulp.add(index)
                continue
            if not all(index in p for p in later):
                transient += 1
                continue
            memory.append((key, index, pair))
    calls, volatile_calls = [], []
    for key in common:
        row = names.get(key, ["?"])
        kinds_ret = next((types[n] for n in row if n in types), None)
        ret_kind, kinds = kinds_ret if kinds_ret else (None, None)
        local = matcher.is_local(row)
        for i in sorted(set(oracle.calls.get(key, {})) & set(port.calls.get(key, {}))):
            differences = compare_call(matcher, kinds, local, oracle.calls[key][i], port.calls[key][i])
            if i in oracle.rets.get(key, {}) and i in port.rets.get(key, {}):
                why = compare_return(
                    matcher,
                    ret_kind,
                    oracle.rets[key][i],
                    port.rets[key][i],
                    int(oracle.calls[key][i]["sp"], 16),
                    int(port.calls[key][i]["sp"], 16),
                )
                if why:
                    differences.append(why)
            changes = (
                reference_call_changes(
                    kinds,
                    ret_kind,
                    oracle.calls[key][i],
                    other.calls.get(key, {}).get(i),
                    oracle.rets.get(key, {}).get(i),
                    other.rets.get(key, {}).get(i),
                )
                if other
                else {}
            )
            for why in differences:
                field = why.split()[0]
                if field in changes:
                    volatile_calls.append(dict(order=key, function=row[0], call=i, why=why, **changes[field]))
                else:
                    calls.append((key, i, why))
    first_mem = memory[0][0] if memory else None
    first_call = calls[0][0] if calls else None
    bad = min(x for x in (first_mem, first_call) if x is not None) if memory or calls else None
    matched = max([k for k in common if bad is None or k < bad], default=0)
    report = {
        "matched_through": matched,
        "of": args.pak,
        "port_points": len(port.points),
        "oracle_points": len(oracle.points),
        "compared": len(common),
        "trace": len(names),
        "first": None,
        "memory": [],
        "calls": [],
        "shim_words": len(shim),
        "transient_words": transient,
        "masked": [[f"0x{lo:08x}", f"0x{hi:08x}", why] for lo, hi, why in ranges],
        "volatile_words": len(volatile),
        "volatile_calls": volatile_calls,
        "boot_copy_words": len(boot_words),
        "setup_words": len(setup),
        "fp_ulp_words": len(ulp),
        "first_memory": None,
        "first_call": None,
        "watch": {"oracle": oracle.watch[:50], "port": port.watch[:50]},
    }
    for key, index, (oh, ph) in memory:
        if index < 0:
            address = RDRAM + (-1 - index) * PAGE
            report["memory"].append(
                {
                    "order": key,
                    "function": names.get(key, ["?"])[0],
                    "page": f"0x{address:08x}",
                    "address": f"0x{address:08x}",
                    "symbol": matcher.symbol(address),
                    "class": "page",
                    "oracle": f"{oh:016x}",
                    "port": f"{ph:016x}",
                }
            )
            continue
        address = RDRAM + index * 4
        w = index % WORDS
        report["memory"].append(
            {
                "order": key,
                "function": names.get(key, ["?"])[0],
                "page": f"0x{address & ~(PAGE - 1):08x}",
                "address": f"0x{address:08x}",
                "symbol": matcher.symbol(address),
                "oracle": words_text(oracle.bytes(oh), w, True),
                "port": words_text(port.bytes(ph), w, False),
            }
        )
    for key, i, why in calls:
        report["calls"].append({"order": key, "function": names.get(key, ["?"])[0], "call": i, "why": why})
    report["first_memory"] = dict(report["memory"][0], kind="memory") if report["memory"] else None
    report["first_call"] = dict(report["calls"][0], kind="call") if report["calls"] else None
    if bad is not None:
        if first_call is not None and (first_mem is None or first_call <= first_mem):
            report["first"] = dict(report["calls"][0], kind="call")
        else:
            report["first"] = dict(report["memory"][0], kind="memory")
    apply_acceptances(report, reviews, common)
    return report


def print_report(report):
    print(
        f"lockstep: matched-through {report['matched_through']}/{report['of']} "
        f"(port {report['port_points']} of {report['trace']} first hits, {report['compared']} compared, "
        f"oracle {report['oracle_points']})"
    )
    first = report["first"]
    if first is None:
        print("lockstep: no divergence")
    elif first["kind"] == "call":
        print(
            f"lockstep: first divergence at order {first['order']} {first['function']} call {first['call']}: "
            f"{first['why']}"
        )
    else:
        print(
            f"lockstep: first divergence at order {first['order']} {first['function']}: page {first['page']} "
            f"{first['symbol']}: oracle {first['oracle']} | port {first['port']}"
        )
    if first is not None and first["kind"] == "memory" and report["first_call"]:
        c = report["first_call"]
        print(f"lockstep: first call divergence at order {c['order']} {c['function']} call {c['call']}: {c['why']}")
    after = report["first_after_accepted"]
    print(
        "lockstep: first after accepted rows: "
        + (
            f"order {after['order']} {after['function']} {after.get('symbol', after.get('why', ''))}"
            if after
            else "none"
        )
    )
    print(f"lockstep: matched-through after accepted rows {report['matched_through_after_accepted']}/{report['of']}")
    for row in report["memory"]:
        print(
            f"lockstep: memory order {row['order']} {row['function']} {row['symbol']} ({row['address']}): "
            f"oracle {row['oracle']} | port {row['port']}" + acceptance_text(row)
        )
    for category in ("argument", "return"):
        for row in report["calls"]:
            if ("return" if row["why"].startswith("return ") else "argument") == category:
                print(
                    f"lockstep: {category} order {row['order']} {row['function']} #{row['call']}: "
                    f"{row['why']}" + acceptance_text(row)
                )
    for row in report.get("volatile_calls", []):
        print(
            f"lockstep: volatile call order {row['order']} {row['function']} #{row['call']}: "
            f"{row['why']} (independent references: {row['oracle']} vs {row['oracle2']})"
        )
    for row in report["unmatched_accepts"]:
        print(f"lockstep: acceptance not observed: {row['kind']} order {row['order']} {row['function']} {row['site']}")
    print(
        f"lockstep: {len(report['memory'])} persistent memory words, {len(report['calls'])} call divergences listed; "
        f"{report['shim_words']} shim-owned words, {report['transient_words']} transient, "
        f"{report['volatile_words']} volatile, {report['boot_copy_words']} boot-copy masked, "
        f"{report['setup_words']} setup words (differ before the first game function), "
        f"{report['fp_ulp_words']} float words within 4 ulp"
    )


# ---- whole-movie match keyed by poll ----------------------------------------------------------------------------
MATCH_CLASSES = ("input", "vi", "calls", "memory")
RUN_LOCKSTEP, RUN_SIDES, RUN_MATCH = "lockstep", {"oracle": "canonical-be", "port": "native"}, "match.json"
HEX8, HEX16 = re.compile(r"[0-9a-f]{8}"), re.compile(r"[0-9a-f]{16}")


def match_input(oracle, port, polls):
    """Polls whose pad words differ, or that only one side reached."""
    out = []
    for poll in polls:
        o, p = oracle.get(poll), port.get(poll)
        if o is None or p is None or o["pad"] != p["pad"]:
            out.append({"poll": poll, "oracle": o and o["pad"], "port": p and p["pad"]})
    return out


def match_vi(oracle, port, polls):
    """Polls where the VI offset (port vi - oracle vi) changes: each is one retrace the two sides spent differently."""
    out, offset = [], 0
    for poll in polls:
        o, p = oracle.get(poll), port.get(poll)
        if o is None or p is None:
            continue
        now = p["vi"] - o["vi"]
        if now != offset:
            out.append({"poll": poll, "oracle": o["vi"], "port": p["vi"], "offset": now})
            offset = now
    return out


def same_words(matcher, ow, pw):
    return len(ow) == len(pw) and all(
        same_word(matcher, "unknown", int(a, 16), int(b, 16), 0, -PORT_STACK * 4) for a, b in zip(ow, pw, strict=True)
    )


def unrecorded_on_port(oracle, port, last_poll):
    """The functions the oracle calls by last_poll that the port's record holds no call of. The oracle hooks every
    recompiled function, but the port records only the trace table's instrumented bodies: a shim-bound or unlinked
    function, or one entered outside its own frame (an `unframed` line), never shows there, so its absence proves
    nothing and is listed, not counted."""
    recorded = {fn for fn, _ in port.movie_calls}
    return sorted({fn for (fn, _), r in oracle.movie_calls.items() if fn not in recorded and r["poll"] <= last_poll})


def match_calls(matcher, oracle, port, last_poll, types=None):
    """Whole-movie calls keyed by (fn, i), i the function's own call index (every call counted, whoever the caller):
    a function the port calls by last_poll and the oracle, which hooks every recompiled function, never does
    (`missing`), a call both kept in another poll (`poll`) and, for a function types gives a prototype, the arguments
    and return in its ABI slots (`args`, `return`). A call one side kept and the other did not (other callers, other
    keep limits) proves nothing; neither does a call without a prototype, whose arity and float slots are unknown, nor
    a function the port's record cannot show (unrecorded_on_port)."""
    types = types or {}
    out = []
    first_call = {}
    for side, calls in (("oracle", oracle.movie_calls), ("port", port.movie_calls)):
        for (fn, _), r in calls.items():
            at = first_call.setdefault(fn, {}).get(side)
            if at is None or r["poll"] < at:
                first_call[fn][side] = r["poll"]
    for fn, seen in sorted(first_call.items()):
        if set(seen) == {"port"} and seen["port"] <= last_poll:
            out.append({"fn": fn, "i": 0, "poll": seen["port"], "field": "missing", "oracle": False, "port": True})
    for key in sorted(set(oracle.movie_calls) & set(port.movie_calls)):
        oc, pc = oracle.movie_calls[key], port.movie_calls[key]
        fn, i = key
        row = {"fn": fn, "i": i, "caller": oc.get("caller", "?")}
        poll = min(oc["poll"], pc["poll"])
        if poll > last_poll:
            continue
        if oc["poll"] != pc["poll"]:
            out.append(dict(row, poll=poll, field="poll", oracle=oc["poll"], port=pc["poll"]))
        if fn not in types:
            continue
        ret_kind, kinds = types[fn]
        for why in compare_call(matcher, kinds, False, oc, pc):
            out.append(dict(row, poll=poll, field="args", why=why, oracle=oc.get("a"), port=pc.get("a")))
        orr, prr = oracle.movie_rets.get(key), port.movie_rets.get(key)
        if orr and prr:
            why = compare_return(matcher, ret_kind, orr, prr, int(oc["sp"], 16), int(pc["sp"], 16))
            if why:
                out.append(dict(row, poll=poll, field="return", why=why, oracle=orr.get("r"), port=prr.get("r")))
    return sorted(out, key=lambda r: (r["poll"], r["fn"], r["i"]))


def match_memory(matcher, oracle, port, polls, persist):
    """Words that start differing at a poll and still differ at the next persist polls (or to the movie's end).
    Each stream keeps its own baseline, so a poll's page hashes are the previous poll's with this poll's pages.
    Only the pages a poll lists can change which words differ, so each poll costs its listed pages, not the RAM; each
    word's run (differing, since which poll index, poll and values) is an array over the RAM, so a listed page updates
    its 1024 words at once (pp-1p-match lists 2.7M value words in its first 800 polls)."""
    ram = min(oracle.meta["ram"], port.meta["ram"])
    pages = ram // PAGE
    oh, ph = [0] * pages, [0] * pages
    active = np.zeros((pages, WORDS), bool)
    since, poll_of, ov_of, pv_of = (np.zeros((pages, WORDS), np.int64) for _ in range(4))
    runs = []  # (word, start poll, oracle value, port value) arrays of each run recorded
    unknown, page_runs = {}, []  # page -> (start index, poll, hashes) while no dump holds its bytes; recorded page runs

    def listed(side):
        """Each of polls' page list, streamed from the side's file in poll order."""
        stream = side.poll_pages()
        for poll in polls:
            for got, pages in stream:
                if got == poll:
                    yield pages
                    break
            else:
                raise ValueError(f"{side.dir}: poll {poll} missing or out of order")

    for index, (poll, oracle_pages, port_pages) in enumerate(zip(polls, listed(oracle), listed(port), strict=True)):
        changed = set()
        for listing, hashes in ((oracle_pages, oh), (port_pages, ph)):
            for page, h in listing:
                if page < pages:
                    hashes[page] = int(h, 16)
                    changed.add(page)
        for pg in changed:
            now = np.zeros(WORDS, bool)
            differ = oh[pg] != ph[pg]  # one canonical hash: the same words
            o, p = (oracle.bytes(oh[pg]), port.bytes(ph[pg])) if differ else (None, None)
            if differ and (o is None or p is None):  # no dump holds it: the page is the unit, its words still differ
                unknown.setdefault(pg, (index, poll, oh[pg], ph[pg]))
                continue
            start = unknown.pop(pg, None)
            if start is not None and index - start[0] > persist:
                page_runs.append((pg, *start[1:]))
            if differ:
                ws, ovs, pvs, kinds = matcher.page_words(o, p)
                value = kinds == "value"
                ws, ovs, pvs = ws[value], ovs[value], pvs[value]
                now[ws] = True
                fresh = ~active[pg, ws]
                ws, ovs, pvs = ws[fresh], ovs[fresh], pvs[fresh]
            ended = np.flatnonzero(active[pg] & ~now & (index - since[pg] > persist))  # differed past persist polls
            if ended.size:
                runs.append((pg * WORDS + ended, poll_of[pg, ended], ov_of[pg, ended], pv_of[pg, ended]))
            if differ:
                since[pg, ws], poll_of[pg, ws], ov_of[pg, ws], pv_of[pg, ws] = index, poll, ovs, pvs
            active[pg] = now
    left = np.flatnonzero(active)
    runs.append((left, poll_of.ravel()[left], ov_of.ravel()[left], pv_of.ravel()[left]))
    words, starts, ovs, pvs = (np.concatenate(column).tolist() for column in zip(*runs, strict=True))
    out = []
    for i in np.lexsort((words, starts)).tolist():
        address = RDRAM + 4 * words[i]
        out.append({"poll": starts[i], "address": f"{address:08x}", "symbol": matcher.symbol(address), "class": "value",
                    "oracle": f"{ovs[i]:08x}", "port": f"{pvs[i]:08x}"})  # fmt: skip
    page_runs += [(pg, *start[1:]) for pg, start in unknown.items()]
    out += [{"poll": poll, "address": f"{RDRAM + pg * PAGE:08x}", "symbol": matcher.symbol(RDRAM + pg * PAGE),
             "class": "page", "oracle": f"{o:016x}", "port": f"{p:016x}"} for pg, poll, o, p in page_runs]  # fmt: skip
    return sorted(out, key=lambda r: (r["poll"], r["address"], r["class"]))


def dump_request(oracle, port, polls, at):
    """{poll, pages} a dump replay writes: at poll `at`, every page whose two hashes differ and whose bytes a side does
    not hold yet (both sides dump the same pages); None when there is none. The page lists stream from the files."""
    ram = min(oracle.meta["ram"], port.meta["ram"])
    oh, ph = [0] * (ram // PAGE), [0] * (ram // PAGE)
    streams = oracle.poll_pages(), port.poll_pages()
    for poll in polls:
        for stream, hashes in zip(streams, (oh, ph), strict=True):
            listing = next((pages for got, pages in stream if got == poll), None)
            if listing is None:
                raise ValueError(f"poll {poll} missing or out of order")
            for page, h in listing:
                if page < len(hashes):
                    hashes[page] = int(h, 16)
        if poll == at:
            missing = [pg for pg, (o, p) in enumerate(zip(oh, ph, strict=True))
                       if o != p and (oracle.bytes(o) is None or port.bytes(p) is None)]  # fmt: skip
            return {"poll": at, "pages": missing} if missing else None
    return None


def match(args):
    """The whole movie compared poll by poll: per class the ordered persistent divergences, matched-through poll and the
    first divergence."""
    oracle, port, _ = sides(args)
    matcher = Matcher(min(oracle.meta["ram"], port.meta["ram"]), read_symbols(args.symbols),
                      read_exe_functions(args.port_exe), read_exe_data(args.port_exe))  # fmt: skip
    every = sorted(set(oracle.polls) | set(port.polls))
    common = [p for p in every if p in oracle.polls and p in port.polls]
    if common != every[: len(common)]:
        raise ValueError("poll indices skip on one side")
    divergences = {
        "input": match_input(oracle.polls, port.polls, every),
        "vi": match_vi(oracle.polls, port.polls, common),
        "calls": match_calls(matcher, oracle, port, common[-1], read_types(args.types)),
        "memory": match_memory(matcher, oracle, port, common, args.persist),
    }
    first = None
    for cls in MATCH_CLASSES:
        if divergences[cls] and (first is None or divergences[cls][0]["poll"] < first["poll"]):
            first = dict(divergences[cls][0], **{"class": cls})
    last = every[-1]
    pending = [r for r in divergences["memory"] if r["class"] == "page"]
    return {
        "polls": {"oracle": len(oracle.polls), "port": len(port.polls), "common": len(common)},
        "keep": {"oracle": oracle.keep_limit(), "port": port.keep_limit()},
        "unrecorded_on_port": unrecorded_on_port(oracle, port, common[-1]),
        "matched_through_poll": first["poll"] - 1 if first else last,
        "first": first,
        "divergences": divergences,
        "dump": dump_request(oracle, port, common, pending[0]["poll"]) if pending else None,
    }


def sides(args):
    """The two Sides of `match`/`pages` (oracle canonical, port native) and their common polls."""
    oracle, port = Side(args.oracle, lambda r: None), Side(args.port, lambda r: None)
    if not oracle.canonical or port.canonical:
        raise ValueError("--oracle must be the canonical-be side and --port the native one")
    if not oracle.polls or not port.polls:
        raise ValueError("both sides need poll lines (a TAS movie played with lockstep on)")
    return oracle, port, sorted(set(oracle.polls) & set(port.polls))


def pages(args):
    """The dump request of a run: --poll P computes it for that poll, else match.json's `dump`."""
    lockstep = pathlib.Path(args.run) / RUN_LOCKSTEP
    if args.poll is None:
        return json.loads((lockstep / RUN_MATCH).read_text()).get("dump")
    args.oracle, args.port = lockstep / "oracle", lockstep / "port"
    oracle, port, common = sides(args)
    if args.poll not in common:
        raise ValueError(f"poll {args.poll} is not a poll both sides recorded")
    return dump_request(oracle, port, common, args.poll)


# ---- the run record (CONTRACTS.md "Run record") ------------------------------------------------------------------
def check_side(side_dir, name):
    """The problems of one side's lockstep.jsonl and dumps: meta, poll, call/ret lines, and every dump being the
    recorded state (its pages hash to their keys, and to the record's hashes at that poll) (one reader: Side)."""
    try:
        side = Side(side_dir, lambda r: None)
    except (OSError, ValueError, KeyError) as error:
        return [f"lockstep/{name}: {error}"]
    meta, problems = side.meta, []
    for key, want in (("side", name), ("page", PAGE), ("bytes", RUN_SIDES[name]), ("hash", "fnv1a64-le64")):
        if meta.get(key) != want:
            problems.append(f"lockstep/{name} meta.{key}: {meta.get(key)!r}, expected {want!r}")
    if not isinstance(meta.get("ram"), int) or meta["ram"] <= 0 or meta["ram"] % PAGE:
        problems.append(f"lockstep/{name} meta.ram: {meta.get('ram')!r}, expected a positive multiple of {PAGE}")
    polls = sorted(side.polls)
    if not polls or polls != list(range(polls[0], polls[0] + len(polls))):
        problems.append(f"lockstep/{name} poll: indices {polls[:3]}... are not one contiguous run")
    current = {}
    for poll, page, h in side.bad_dumps:
        problems.append(f"lockstep/{name} dump {poll} page {page}: its bytes do not hash to {h:016x}")
    for poll in sorted(set(side.dumps) - set(polls)):
        problems.append(f"lockstep/{name} dump {poll}: no such poll in the record")
    for poll in polls:
        r = side.polls[poll]
        if not isinstance(r.get("vi"), int):
            problems.append(f"lockstep/{name} poll {poll} vi: {r.get('vi')!r}, expected an integer")
        pad = r.get("pad")
        if not isinstance(pad, list) or not pad or not all(isinstance(w, str) and HEX8.fullmatch(w) for w in pad):
            problems.append(f"lockstep/{name} poll {poll} pad: {pad!r}, expected 8-hex-digit words")
    for poll, listing in side.poll_pages():
        for entry in listing:
            if not (isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], int)
                    and isinstance(entry[1], str) and HEX16.fullmatch(entry[1])):  # fmt: skip
                problems.append(f"lockstep/{name} poll {poll} pages: {entry!r}, expected [page, 16-hex hash]")
                continue
            current[entry[0]] = int(entry[1], 16)
        for page, h in sorted(side.dumps.get(poll, {}).items()):
            if current.get(page) != h:
                problems.append(f"lockstep/{name} dump {poll} page {page}: {h:016x}, the record holds "
                                f"{current.get(page, 0):016x} (the replay is not the recorded run)")  # fmt: skip
    for r in [*side.movie_calls.values(), *side.movie_rets.values()]:
        fn, i, caller, o = r.get("fn"), r.get("i"), r.get("caller", ""), r.get("o", 0 if r["k"] == "ret" else None)
        if not isinstance(fn, str) or type(i) is not int or not isinstance(caller, str) or type(o) is not int:
            problems.append(
                f"lockstep/{name} {r['k']}: {fn!r}/{i!r}/{caller!r}/{o!r}, expected fn, call index i, caller, ordinal"
            )
    return problems


def check_match(path):
    """(problems, report) of RUN/lockstep/match.json, the `match` report: every field well-formed, every divergence row
    a dict at an integer poll (a calls row names fn, call index i and field; a memory row is a word or a page with
    its two hex values), and `first` null exactly when no class holds a divergence. report is None when unreadable."""
    name = f"lockstep/{RUN_MATCH}"
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        return [f"{name}: {error}"], None
    if not isinstance(report, dict):
        return [f"{name}: {type(report).__name__}, expected an object"], None
    problems = []
    polls = report.get("polls")
    if not isinstance(polls, dict) or sorted(polls) != ["common", "oracle", "port"]:
        problems.append(f"{name} polls: {polls!r}, expected oracle, port, common counts")
    elif not all(type(n) is int and n >= 0 for n in polls.values()):
        problems.append(f"{name} polls: {polls!r}, expected counts (integers, 0 or more)")
    if type(report.get("matched_through_poll")) is not int:
        problems.append(f"{name} matched_through_poll: expected an integer")
    first = report.get("first")
    if first is not None and (
        not isinstance(first, dict) or first.get("class") not in MATCH_CLASSES or type(first.get("poll")) is not int
    ):
        problems.append(f"{name} first: {first!r}, expected null or {{class, poll, ...}}")
    divergences = report.get("divergences")
    if (
        not isinstance(divergences, dict)
        or sorted(divergences) != sorted(MATCH_CLASSES)
        or not all(isinstance(v, list) for v in divergences.values())
    ):
        problems.append(f"{name} divergences: expected one list per class {', '.join(MATCH_CLASSES)}")
    else:
        for cls in MATCH_CLASSES:
            for index, row in enumerate(divergences[cls]):
                if why := divergence_problem(cls, row):
                    problems.append(f"{name} divergences.{cls}[{index}]: {row!r}, expected {why}")
                    break
        if (first is None) != (not any(divergences.values())):
            problems.append(f"{name} first: {first!r}, but the divergences hold {sum(map(len, divergences.values()))}")
    dump = report.get("dump", "missing")
    if dump is not None and not (
        isinstance(dump, dict) and type(dump.get("poll")) is int and isinstance(dump.get("pages"), list)
        and dump["pages"] and all(type(pg) is int for pg in dump["pages"])
    ):  # fmt: skip
        problems.append(f"{name} dump: {dump!r}, expected null or {{poll, pages}}")
    return problems, report


def divergence_problem(cls, row):
    """What a match.json divergence row of class `cls` lacks (CONTRACTS.md Run record), or None when it is whole."""
    if not isinstance(row, dict) or type(row.get("poll")) is not int:
        return "a row with its integer poll"
    if cls == "calls" and not (
        isinstance(row.get("fn"), str) and type(row.get("i")) is int and row.get("field") in CALL_FIELDS
    ):  # fmt: skip
        return f"fn, call index i and field ({', '.join(CALL_FIELDS)})"
    if cls == "memory":
        digits = {"value": HEX8, "page": HEX16}.get(row.get("class"))
        if digits is None or not all(isinstance(row.get(s), str) and digits.fullmatch(row[s]) for s in RUN_SIDES):
            return "class value (8-hex oracle/port) or page (16-hex oracle/port)"
    return None


def check_calls(path):
    """The problems of RUN/lockstep/port/calls.jsonl: the port's calls meta first, then `call` lines (fn, caller,
    ordinal o, poll frame, argument words a), `ret` lines (fn, caller, o, return words r) and `unframed` lines (trace.c:
    a function's first call outside its own frame, its trace order and 8-hex pc). A last line cut by a killed run is
    dropped, as every reader drops it; any other unreadable line is a problem."""
    name = f"{RUN_LOCKSTEP}/port/{RUN_CALLS}"
    if not path.is_file():
        return [f"{name}: missing (every play writes the port's calls)"]
    lines = path.read_text(errors="replace").split("\n")
    problems = []
    for number, line in enumerate(lines, 1):
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            if number < len(lines):  # a line with a newline after it was written whole
                problems.append(f"{name} line {number}: unreadable")
            continue
        kind = r.get("k") if isinstance(r, dict) else None
        if number == 1:
            if kind != "meta" or r.get("side") != "port" or r.get("mode") != "calls":
                problems.append(f"{name} line 1: {line[:120]}, expected the port's calls meta")
            continue
        if kind == "unframed":
            if type(r.get("order")) is not int or not (isinstance(r.get("pc"), str) and HEX8.fullmatch(r["pc"])):
                problems.append(f"{name} line {number}: an unframed line without its integer order and 8-hex pc")
        elif kind not in ("call", "ret"):
            problems.append(f"{name} line {number}: k {kind!r}, expected call, ret or unframed")
        elif not (isinstance(r.get("fn"), str) and isinstance(r.get("caller"), str) and type(r.get("o")) is int):
            problems.append(f"{name} line {number}: expected fn, caller and ordinal o")
        elif kind == "call" and (type(r.get("frame")) is not int or not isinstance(r.get("a"), list)):
            problems.append(f"{name} line {number}: a call without its poll frame and argument words a")
        elif kind == "ret" and not isinstance(r.get("r"), list):
            problems.append(f"{name} line {number}: a ret without its return words r")
        if len(problems) == RUN_PROBLEMS_SHOWN:
            problems.append(f"{name}: later lines not checked")
            break
    return problems


RUN_REACH, RUN_RECORD, RUN_COVERAGE = "reach.bin", "run.json", "coverage.tsv"
RUN_CALLS, RUN_REPLAY, RUN_STREAM, RUN_STREAM_INDEX = "calls.jsonl", "replay", "stream", "index.json"
RUN_AUDIO = ("audio.pcm", "audio-oracle.pcm")  # each with <name>.rate once it holds sound
RUN_AUDIO_RATES = range(8000, 192001)
RUN_PROBLEMS_SHOWN = 8  # problems listed per file before the rest of it is left unread
# Retired paths (CONTRACTS.md Run record): present means a writer from before the record they moved into still runs.
RUN_RETIRED = {
    "polls.raw": "the pads the guest read are lockstep/port/lockstep.jsonl poll lines",
    RUN_CALLS: f"the port's calls are {RUN_LOCKSTEP}/port/{RUN_CALLS}",
    f"{RUN_LOCKSTEP}/port/pages.bin": "the record is hashes only; page bytes come from a dump replay",
    f"{RUN_LOCKSTEP}/oracle/pages.bin": "the record is hashes only; page bytes come from a dump replay",
}
CALL_FIELDS = ("missing", "poll", "args", "return")
SHA256 = re.compile(r"[0-9a-f]{64}")
COVERAGE_HEADER = ("function", "reached", "calls", "diverged", "equal")


def run_site(run):
    """The baked site a run under <tas.runs>/plays/RUN maps its reach to: <tas.runs>/site."""
    return pathlib.Path(run).resolve().parent.parent / "site"


def read_run_record(run):
    """RUN/run.json as a dict, or ValueError naming why it is not one."""
    path = pathlib.Path(run) / RUN_RECORD
    if not path.is_file():
        raise ValueError(f"{RUN_RECORD}: missing (every play writes its record)")
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"{RUN_RECORD}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"{RUN_RECORD}: {type(record).__name__}, expected an object")
    return record


def census_of(record, beside):
    """run.json's census {fnrows, sha256, rows}, each well-formed, or ValueError naming the field; `beside` is what the
    run holds that needs it (reach.bin, coverage.tsv), named when the census is missing."""
    census = record.get("census")
    if census is None:
        raise ValueError(f"{RUN_RECORD} census: missing" + (f" beside {beside}" if beside else ""))
    if not isinstance(census, dict) or sorted(census) != ["fnrows", "rows", "sha256"]:
        raise ValueError(f"{RUN_RECORD} census: {census!r}, expected {{fnrows, sha256, rows}}")
    fnrows = census["fnrows"]
    if not isinstance(fnrows, str) or not fnrows or fnrows in (".", "..") or "/" in fnrows or "\0" in fnrows:
        raise ValueError(f"{RUN_RECORD} census fnrows: {fnrows!r}, expected a file name in the site")
    if not isinstance(census["sha256"], str) or not SHA256.fullmatch(census["sha256"]):
        raise ValueError(f"{RUN_RECORD} census sha256: {census['sha256']!r}, expected 64 lowercase hex digits")
    if type(census["rows"]) is not int or census["rows"] <= 0:
        raise ValueError(f"{RUN_RECORD} census rows: {census['rows']!r}, expected a positive row count")
    return census


def read_reach(run, rows):
    """RUN/reach.bin, a bare bitset over `rows` census rows: ceil(rows/8) bytes, no bit set past the last row."""
    try:
        bits = (pathlib.Path(run) / RUN_REACH).read_bytes()
    except OSError as error:
        raise ValueError(f"{RUN_REACH}: {error.strerror or error} (the census names a reach)") from error
    if len(bits) != (rows + 7) // 8:
        raise ValueError(f"{RUN_REACH}: {len(bits)} bytes, expected ceil({rows}/8) = {(rows + 7) // 8}")
    if rows % 8 and bits[-1] >> (rows % 8):
        raise ValueError(f"{RUN_REACH}: pad bits set past row {rows}")
    return bits


def read_census(run):
    """(census, fnrows rows, reach bits) of a run, or ValueError naming what breaks it: run.json's census
    {fnrows, sha256, rows} over the site's fnrows file of that name with that sha256 and row count, and reach.bin a
    bare bitset of ceil(rows/8) bytes with no bit set past the last row. reach.bin without a census is a refusal."""
    run = pathlib.Path(run)
    census = census_of(read_run_record(run), RUN_REACH if (run / RUN_REACH).exists() else None)
    try:
        data = (run_site(run) / census["fnrows"]).read_bytes()
    except OSError as error:
        raise ValueError(f"census fnrows: {error}") from error
    if hashlib.sha256(data).hexdigest() != census["sha256"]:
        raise ValueError(f"census sha256 {census['sha256']}: the site's {census['fnrows']} no longer has it")
    names = data.decode().splitlines()
    if len(names) != census["rows"]:
        raise ValueError(f"census rows {census['rows']}: the site's {census['fnrows']} has {len(names)}")
    return census, names, read_reach(run, len(names))


def check_census(run, record):
    """The problems of RUN/reach.bin and its run.json census; a run with neither has no reach to check."""
    run = pathlib.Path(run)
    if not (run / RUN_REACH).exists() and "census" not in record:
        return []
    try:
        read_census(run)
    except ValueError as error:
        return [str(error)]
    return []


def join(run, names, bits):
    """The coverage.tsv rows of `names` (census rows in fnrows order) over reach `bits`: reached from reach.bin, calls
    the lockstep/port/calls.jsonl `call` lines naming it, diverged the match.json divergences.calls entries naming it,
    equal `yes` only when reached, calls > 0 and diverged = 0. ValueError when calls.jsonl or match.json is missing."""
    lockstep = pathlib.Path(run) / RUN_LOCKSTEP
    calls_path, match_path = lockstep / "port" / RUN_CALLS, lockstep / RUN_MATCH
    for path in (calls_path, match_path):
        if not path.is_file():
            raise ValueError(f"{path.relative_to(run)}: missing")
    report = json.loads(match_path.read_text())
    calls, diverged = {}, {}
    for line in calls_path.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue  # a last line cut by a killed run
        if r.get("k") == "call" and "o" in r:
            calls[r["fn"]] = calls.get(r["fn"], 0) + 1
    for row in report["divergences"]["calls"]:
        diverged[row["fn"]] = diverged.get(row["fn"], 0) + 1
    rows = []
    for index, name in enumerate(names):
        hit = bool(bits[index >> 3] >> (index & 7) & 1)
        n, d = calls.get(name, 0), diverged.get(name, 0)
        rows.append((name, "yes" if hit else "no", n, d, "yes" if hit and n and not d else "no"))
    return rows


def coverage(run):
    """The per-function parity join (CONTRACTS.md Run record, coverage.tsv): one row per census row in fnrows order
    (`join`). Memory, vi and input divergences are movie-level and named by no function. ValueError when an input is
    missing: no empty join."""
    census, names, bits = read_census(run)
    return census, join(pathlib.Path(run), names, bits)


def write_coverage(run):
    """RUN/coverage.tsv from `coverage`; returns its summary line `census <sha256> rows=<n> reached=<r> equal=<e>`."""
    census, rows = coverage(run)
    text = "\t".join(COVERAGE_HEADER) + "\n" + "".join("\t".join(map(str, row)) + "\n" for row in rows)
    tmp = pathlib.Path(run) / (RUN_COVERAGE + ".tmp")
    tmp.write_text(text)
    tmp.replace(pathlib.Path(run) / RUN_COVERAGE)
    reached, equal = sum(r[1] == "yes" for r in rows), sum(r[4] == "yes" for r in rows)
    return f"census {census['sha256']} rows={len(rows)} reached={reached} equal={equal}"


def check_coverage(run, record):
    """The problems of RUN/coverage.tsv, proved from the run folder alone: its function column is the census rows (one
    name and a newline each hash to the census sha256), and every row is what `join` computes from this run's
    reach.bin, calls.jsonl and match.json. A run with no coverage.tsv has no join to check."""
    path = pathlib.Path(run) / RUN_COVERAGE
    if not path.exists():
        return []
    try:
        census = census_of(record, RUN_COVERAGE)
    except ValueError as error:
        return [f"{RUN_COVERAGE}: no census to prove it over ({error})"]
    lines = path.read_text(errors="replace").split("\n")
    if lines[-1]:
        return [f"{RUN_COVERAGE}: its last row is cut (no newline)"]
    rows = [line.split("\t") for line in lines[:-1]]
    if not rows or tuple(rows[0]) != COVERAGE_HEADER:
        return [f"{RUN_COVERAGE} header: {lines[0]!r}, expected {chr(9).join(COVERAGE_HEADER)!r}"]
    rows = rows[1:]
    if len(rows) != census["rows"]:
        return [f"{RUN_COVERAGE}: {len(rows)} rows, the census has {census['rows']}"]
    names = [row[0] for row in rows]
    if hashlib.sha256("".join(name + "\n" for name in names).encode()).hexdigest() != census["sha256"]:
        return [f"{RUN_COVERAGE} function column: not the census rows in fnrows order (sha256 {census['sha256']})"]
    try:
        want = join(run, names, read_reach(run, len(names)))
    except (ValueError, KeyError, TypeError) as error:
        return [f"{RUN_COVERAGE}: the join cannot be recomputed: {error}"]
    for number, (got, row) in enumerate(zip(rows, want, strict=True), 2):
        if got != [str(value) for value in row]:
            have, expected = "\t".join(got), "\t".join(map(str, row))
            return [f"{RUN_COVERAGE} line {number}: {have!r}, the join gives {expected!r}"]
    return []


def check_movie(run):
    """The problems of RUN/movie.raw: one line per poll, each pad `BUTTONS STICK_X STICK_Y` (hex buttons up to ffff,
    sticks signed bytes); a movie with no poll is no run."""
    try:
        text = (pathlib.Path(run) / "movie.raw").read_text()
    except (OSError, UnicodeDecodeError) as error:
        return [f"movie.raw: {error}"]
    if not text.strip():
        return ["movie.raw: no polls"]
    if not text.endswith("\n"):
        return ["movie.raw: its last poll is cut (no newline)"]
    for number, line in enumerate(text.split("\n")[:-1], 1):
        words = line.split()
        try:
            pads = [(int(words[i], 16), int(words[i + 1]), int(words[i + 2])) for i in range(0, len(words), 3)]
        except (ValueError, IndexError):
            pads = None
        if not pads or not all(0 <= b <= 0xFFFF and -128 <= x <= 127 and -128 <= y <= 127 for b, x, y in pads):
            return [f"movie.raw line {number}: {line!r}, expected BUTTONS STICK_X STICK_Y per pad"]
    return []


def check_oracle_lockstep(record, report):
    """The problems of run.json `oracle.lockstep` (the play's copy of match's result): exit 0 matched, 1 diverged, 2
    refused; for 0 and 1 its matched-through poll and first divergence are match.json's."""
    oracle = record.get("oracle")
    if not isinstance(oracle, dict) or "lockstep" not in oracle:
        return []
    found, name = oracle["lockstep"], f"{RUN_RECORD} oracle.lockstep"
    if not isinstance(found, dict) or found.get("exit") not in (0, 1, 2) or type(found.get("exit")) is not int:
        return [f"{name}: {found!r}, expected {{exit 0, 1 or 2, ...}}"]
    if found["exit"] == 2 or report is None:  # refused: no report to agree with; an unreadable one is named already
        return []
    problems = []
    for key in ("matched_through_poll", "first"):
        if found.get(key) != report.get(key):
            problems.append(f"{name} {key}: {found.get(key)!r}, match.json holds {report.get(key)!r}")
    if (found["exit"] == 1) != (report.get("first") is not None):
        problems.append(f"{name} exit: {found['exit']}, match.json first is {report.get('first')!r}")
    return problems


def check_replays(run):
    """The problems of RUN/lockstep/replay: one folder per dump replay, named by the poll it replayed to."""
    folder = pathlib.Path(run) / RUN_LOCKSTEP / RUN_REPLAY
    entries = sorted(folder.iterdir()) if folder.is_dir() else []
    return [f"{RUN_LOCKSTEP}/{RUN_REPLAY}/{e.name}: expected a folder named by the poll a dump replay ran to"
            for e in entries if not (e.is_dir() and e.name.isdigit())]  # fmt: skip


def check_stream(run):
    """The problems of RUN/stream: index.json {run, sides, polls, inputs, every} and a picture per side per poll."""
    run = pathlib.Path(run)
    folder = run / RUN_STREAM
    if not folder.exists():
        return []
    name = f"{RUN_STREAM}/{RUN_STREAM_INDEX}"
    try:
        index = json.loads((folder / RUN_STREAM_INDEX).read_text())
    except (OSError, ValueError) as error:
        return [f"{name}: {error}"]
    if not isinstance(index, dict) or sorted(index) != ["every", "inputs", "polls", "run", "sides"]:
        return [f"{name}: expected {{run, sides, polls, inputs, every}}"]
    sides, polls, inputs = index["sides"], index["polls"], index["inputs"]
    if index["run"] != run.name:
        return [f"{name} run: {index['run']!r}, expected {run.name!r}"]
    if not isinstance(sides, list) or not sides or not set(sides) <= set(RUN_SIDES) or len(set(sides)) < len(sides):
        return [f"{name} sides: {sides!r}, expected port and/or oracle"]
    if not isinstance(polls, list) or not polls or not all(type(p) is int and p >= 0 for p in polls) or (
        polls != sorted(set(polls))
    ):  # fmt: skip
        return [f"{name} polls: expected ascending poll indices"]
    if type(index["every"]) is not int or index["every"] <= 0:
        return [f"{name} every: {index['every']!r}, expected a positive poll interval"]
    if not isinstance(inputs, list) or len(inputs) != len(polls) or not all(
        i is None or (isinstance(i, list) and len(i) == 3 and all(type(v) is int for v in i)) for i in inputs
    ):  # fmt: skip
        return [f"{name} inputs: expected [buttons, x, y] or null per poll"]
    for poll in polls:
        for side in sides:
            if not (folder / side / f"{poll}.jpg").is_file():
                return [f"{RUN_STREAM}/{side}/{poll}.jpg: missing (index.json lists the poll)"]
    return []


def check_audio(run):
    """The problems of the run's sound: each pcm whole 16-bit stereo frames, and once it holds sound, its .rate one
    integer sample rate."""
    problems = []
    for name in RUN_AUDIO:
        pcm = pathlib.Path(run) / name
        if not pcm.exists():
            continue
        size = pcm.stat().st_size
        if size % 4:
            problems.append(f"{name}: {size} bytes, expected whole 16-bit stereo frames (4 bytes each)")
        if not size:
            continue
        try:
            rate = int((pcm.parent / (name + ".rate")).read_text())
        except (OSError, ValueError) as error:
            problems.append(f"{name}.rate: {error}, expected the sample rate of {name}")
            continue
        if rate not in RUN_AUDIO_RATES:
            problems.append(f"{name}.rate: {rate}, expected {RUN_AUDIO_RATES.start}..{RUN_AUDIO_RATES.stop - 1} Hz")
    return problems


def check_run(run):
    """{run, problems}: the run record under RUN checked field by field (CONTRACTS.md Run record), each field present
    and well-formed or named; an empty list is a run that holds."""
    run = pathlib.Path(run)
    if not (run / "movie.raw").is_file():
        return {"run": str(run), "problems": ["movie.raw: missing (the inputs the run played)"]}
    problems = check_movie(run)
    problems += [f"{path}: retired ({why})" for path, why in RUN_RETIRED.items() if (run / path).exists()]
    try:
        record = read_run_record(run)
    except ValueError as error:
        problems.append(str(error))
        record = None
    for name in RUN_SIDES:
        problems += check_side(run / RUN_LOCKSTEP / name, name)
    problems += check_calls(run / RUN_LOCKSTEP / "port" / RUN_CALLS)
    found, report = check_match(run / RUN_LOCKSTEP / RUN_MATCH)
    problems += found
    problems += check_replays(run)
    if record is not None:
        problems += check_oracle_lockstep(record, report)
        problems += check_census(run, record)
        problems += check_coverage(run, record)
    problems += check_stream(run)
    problems += check_audio(run)
    return {"run": str(run), "problems": problems}


def print_match(report, shown=8):
    counts = report["polls"]
    print(f"lockstep match: polls oracle {counts['oracle']}, port {counts['port']}, common {counts['common']}")
    print(f"matched through poll {report['matched_through_poll']}")
    first = report["first"]
    print("first divergence: " + (json.dumps(first, sort_keys=True) if first else "none"))
    dump = report.get("dump")
    print(
        "dump: " + (f"poll {dump['poll']} pages {dump['pages']}" if dump else "none (every differing page has bytes)")
    )
    for cls in MATCH_CLASSES:
        rows = report["divergences"][cls]
        print(f"{cls}: {len(rows)}")
        for row in rows[:shown]:
            print("  " + json.dumps(row, sort_keys=True))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    t = sub.add_parser("types", help="argument and return kinds of the trace functions (gcc -aux-info)")
    t.add_argument("--build", required=True)
    t.add_argument("--trace", required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--compiler", default=os.environ.get("CC", "gcc"))
    t.add_argument("--jobs", type=int, default=4)
    c = sub.add_parser("compare", help="the first divergence and the matched-through number")
    c.add_argument("--oracle", required=True)
    c.add_argument("--port", required=True)
    c.add_argument("--trace", required=True)
    c.add_argument("--symbols")
    c.add_argument("--port-exe")
    c.add_argument("--types")
    c.add_argument("--ignore")
    c.add_argument("--oracle2")
    c.add_argument("--rom", help="the ROM, to mask IPL3's boot copy the port never makes")
    c.add_argument("--persist", type=int, default=2)
    c.add_argument("--pak", type=int, default=708)
    c.add_argument(
        "--accept",
        default=str(portdir.fact("lockstep-accept.tsv")),
        help="reviewed exact differences with reasons (kept visible in the report)",
    )
    c.add_argument("--json")
    m = sub.add_parser("match", help="a whole TAS movie compared poll by poll: divergences per class")
    m.add_argument("--run", help="a run record: RUN/lockstep/{oracle,port} in, RUN/lockstep/match.json out")
    m.add_argument("--oracle")
    m.add_argument("--port")
    m.add_argument("--symbols")
    m.add_argument("--port-exe")
    m.add_argument("--persist", type=int, default=2)
    m.add_argument("--types", help="types.tsv (lockstep.py types): calls of the functions it types compare their args")
    m.add_argument("--json")
    k = sub.add_parser("check", help="a run record validated field by field (CONTRACTS.md Run record)")
    k.add_argument("run")
    v = sub.add_parser("coverage", help="the per-function parity join of a run: RUN/coverage.tsv (CONTRACTS.md)")
    v.add_argument("--run", required=True, help="a run record with reach.bin, its census, calls.jsonl and match.json")
    d = sub.add_parser("pages", help="the dump request of a run record: {poll, pages} a dump replay writes, or null")
    d.add_argument("--run", required=True)
    d.add_argument("--poll", type=int, help="this poll (default: the one match.json names)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "types":
            found, units = build_types(args.build, args.trace, args.out, args.compiler, args.jobs)
            print(f"lockstep: types for {found} trace functions from {units} units -> {args.out}")
            return 0
        if args.command == "check":
            result = check_run(args.run)
            for problem in result["problems"]:
                print(f"lockstep check: {problem}")
            print(f"lockstep check: {args.run}: {'holds' if not result['problems'] else 'breaks the contract'}")
            return 1 if result["problems"] else 0
        if args.command == "coverage":
            summary = write_coverage(args.run)  # a refusal writes no join and names none
            print(f"lockstep coverage: {pathlib.Path(args.run) / RUN_COVERAGE}")
            print(summary)
            return 0
        if args.command == "pages":
            print(json.dumps(pages(args)))
            return 0
        if args.command == "match":
            if args.run:
                if args.oracle or args.port or args.json:
                    raise ValueError("--run names the sides and the report; leave out --oracle, --port and --json")
                lockstep = pathlib.Path(args.run) / RUN_LOCKSTEP
                args.oracle, args.port, args.json = lockstep / "oracle", lockstep / "port", lockstep / RUN_MATCH
            elif not (args.oracle and args.port):
                raise ValueError("match needs --run RUN, or both --oracle and --port")
            report = match(args)
            print_match(report)
            if args.json:
                pathlib.Path(args.json).write_text(json.dumps(report, indent=1) + "\n")
            return 1 if report["first"] else 0
        report = compare(args)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f"lockstep: {error}", file=sys.stderr)
        return 2
    print_report(report)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, indent=1) + "\n")
    return 1 if report["first_after_accepted"] else 0


if __name__ == "__main__":
    sys.exit(main())
