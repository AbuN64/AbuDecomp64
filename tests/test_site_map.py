"""The Runner site map region: flag bitmask, filter selection and colours (site_map, static/site/map.js)."""

import itertools
import json
import subprocess
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from abu_decomp import site_core, site_map

ROOT = Path(__file__).resolve().parents[1] / "src" / "abu_decomp"
BIT = site_core.FILTER_BITS
COLS = ["name", "rom", "size", "state", "file", "reach", "foundation", "behaviour"]
FILES = ["src/game/LEVEL/a.CPP", "src/game/MENU_SCREEN/b.CPP", "src/os/c.CPP"]
ROWS = [
    ["fn_a", 0, 64, "named", 0, "both", 0, "behaves"],
    ["fn_b", 100, 32, "exact", 1, "port", 0, "differs-args"],
    ["fn_c", 200, 16, "asm", 0, "oracle", 0, ""],
    ["fn_d", 300, 8, "fuzzy", 2, "none", 0, "missing"],
]


def bits(*keys: str) -> int:
    return sum(BIT[key] for key in keys)


class FlagsTest(unittest.TestCase):
    def test_every_filter_has_its_own_bit(self) -> None:
        self.assertEqual(len(set(BIT.values())), len(BIT))
        self.assertTrue(all(bit & (bit - 1) == 0 for bit in BIT.values()))

    def test_state_reach_and_behaviour_set_their_bits(self) -> None:
        self.assertEqual(site_map.flags_of("named", "both", "behaves"), bits("named", "exercised", "behaves"))
        self.assertEqual(site_map.flags_of("exact", "port", "differs-args"), bits("exact", "exercised", "differs"))
        self.assertEqual(site_map.flags_of("exact", "port", "differs-result"), bits("exact", "exercised", "differs"))
        self.assertEqual(site_map.flags_of("asm", "oracle", ""), bits("asm", "original-only"))

    def test_fuzzy_unreached_and_unjudged_set_nothing(self) -> None:
        self.assertEqual(site_map.flags_of("fuzzy", "none", "missing"), 0)
        self.assertEqual(site_map.flags_of("fuzzy", "none", "port-only"), 0)

    def test_feedback_and_stream_bits(self) -> None:
        self.assertEqual(site_map.flags_of("asm", "none", "", feedback=True), bits("asm", "feedback"))
        self.assertEqual(site_map.flags_of("asm", "none", "", streamed=True), bits("asm", "streamed"))

    def test_reach_bitset_names_its_rows_and_refuses_a_wrong_size(self) -> None:
        self.assertEqual(site_map.reach_rows(bytes([0b00000101, 0b00000010]), 10), {0, 2, 9})
        self.assertEqual(site_map.reach_rows(bytes([0xFF]), 10), set())


class SelectionTest(unittest.TestCase):
    def test_all_unchecked_is_all_dark(self) -> None:
        for flags in (0, bits("named"), (1 << len(BIT)) - 1):
            self.assertFalse(site_map.lit(flags, set()))

    def test_a_box_lights_when_any_checked_filter_selects_it(self) -> None:
        flags = bits("named", "behaves")
        self.assertTrue(site_map.lit(flags, {"named"}))
        self.assertTrue(site_map.lit(flags, {"asm", "behaves"}))
        self.assertFalse(site_map.lit(flags, {"asm", "differs"}))

    def test_an_unknown_filter_is_refused(self) -> None:
        with self.assertRaises(KeyError):
            site_map.lit(1, {"nope"})


class ColourTest(unittest.TestCase):
    def test_progress_colours_by_state(self) -> None:
        for state in ("asm", "fuzzy", "exact", "named"):
            self.assertEqual(site_map.colour_key(state, 0), state)
        self.assertEqual(site_map.colour_key("unknown", 0), "asm")

    def test_a_judged_call_overrides_the_state_and_differs_wins(self) -> None:
        self.assertEqual(site_map.colour_key("named", bits("behaves")), "behaves")
        self.assertEqual(site_map.colour_key("asm", bits("differs")), "differs")
        self.assertEqual(site_map.colour_key("named", bits("behaves", "differs")), "differs")

    def test_every_colour_key_has_a_shell_variable(self) -> None:
        html = (ROOT / "runner.html").read_text()
        for variable in site_map.COLOURS.values():
            self.assertIn(f"{variable}:", html)


class BuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name)
        site = self.runs / "site"
        site.mkdir()
        (site / "fns.abc.json").write_text(json.dumps({"ref": "r1", "files": FILES, "cols": COLS, "rows": ROWS}))
        (site / "manifest.json").write_text(json.dumps({"fns": "fns.abc.json"}))
        self.server = SimpleNamespace(cfg={"runs": self.runs}, site=site)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def reach(self, run: str, rows: list[int], *, stream: bool = False) -> None:
        folder = self.runs / "plays" / run
        folder.mkdir(parents=True)
        body = bytearray((len(ROWS) + 7) // 8)
        for i in rows:
            body[i >> 3] |= 1 << (i & 7)
        (folder / "reach.bin").write_bytes(bytes(body))
        if stream:
            (folder / "stream").mkdir()
            (folder / "stream" / "index.json").write_text("{}")

    def feedback(self, run: str) -> None:
        folder = self.runs / "feedback" / f"fb-{run}"
        folder.mkdir(parents=True)
        (folder / "feedback.json").write_text(json.dumps({"run": run}))

    def rows(self) -> list[list]:
        status, body = site_map.answer(self.server, "GET", "", "", b"")
        self.assertEqual(status, HTTPStatus.OK)
        return body["rows"]

    def test_rows_are_name_size_state_flags_region_in_rom_order(self) -> None:
        self.assertEqual(
            self.rows(),
            [
                ["fn_a", 64, "named", bits("named", "exercised", "behaves"), 0],
                ["fn_b", 32, "exact", bits("exact", "exercised", "differs"), 1],
                ["fn_c", 16, "asm", bits("asm", "original-only"), 0],
                ["fn_d", 8, "fuzzy", 0, 2],
            ],
        )

    def test_regions_are_source_folders_in_proper_case_by_first_rom_address(self) -> None:
        _, body = site_map.answer(self.server, "GET", "", "", b"")
        self.assertEqual(
            body["regions"],
            [
                {"key": "src/game/LEVEL", "label": "Level", "count": 2, "bytes": 80, "rom": 0},
                {"key": "src/game/MENU_SCREEN", "label": "Menu Screen", "count": 1, "bytes": 32, "rom": 100},
                {"key": "src/os", "label": "Os", "count": 1, "bytes": 8, "rom": 300},
            ],
        )

    def test_blocks_cover_every_row_once_in_rom_order(self) -> None:
        _, body = site_map.answer(self.server, "GET", "", "", b"")
        blocks = body["blocks"]
        self.assertEqual(blocks[0]["start"], 0)
        self.assertEqual(blocks[-1]["end"], len(body["rows"]))
        self.assertTrue(all(a["end"] == b["start"] for a, b in itertools.pairwise(blocks)))
        self.assertEqual(sum(b["bytes"] for b in blocks), sum(row[1] for row in body["rows"]))
        self.assertEqual([b["rom"] for b in blocks], sorted(b["rom"] for b in blocks))

    def test_blocks_hold_about_equal_bytes_and_never_exceed_the_count(self) -> None:
        sizes = [10] * 100
        blocks = site_map.blocks_of(sizes, list(range(0, 1000, 10)), [0] * 100, 10)
        self.assertEqual(len(blocks), 10)
        self.assertEqual({b["bytes"] for b in blocks}, {100})
        self.assertEqual([(b["start"], b["end"]) for b in blocks][:2], [(0, 10), (10, 20)])
        fewer = site_map.blocks_of([5, 7, 9], [0, 5, 12], [0, 1, 1], 160)
        got = [(b["start"], b["end"], b["rom"], b["regions"]) for b in fewer]
        self.assertEqual(got, [(0, 1, 0, [0]), (1, 2, 5, [1]), (2, 3, 12, [1])])
        self.assertEqual(site_map.blocks_of([], [], [], 4), [])

    def test_a_row_bigger_than_the_share_is_a_block_alone_and_regions_are_listed_once(self) -> None:
        blocks = site_map.blocks_of([1000, 1, 1, 1], [0, 1000, 1001, 1002], [0, 1, 2, 1], 4)
        self.assertEqual([(b["start"], b["end"], b["regions"]) for b in blocks], [(0, 1, [0]), (1, 4, [1, 2])])
        merged = site_map.blocks_of([1, 1, 1, 1], [0, 1, 2, 3], [0, 1, 0, 1], 1)
        self.assertEqual(merged[0]["regions"], [0, 1])

    def test_two_folders_with_one_last_part_are_told_apart_by_their_parent(self) -> None:
        files = ["src/game/UTIL/a.CPP", "src/os/UTIL/b.CPP"]
        rows = [["x", 0, 4, "asm", 0, "none", 0, ""], ["y", 8, 4, "asm", 1, "none", 0, ""]]
        at = {name: COLS.index(name) for name in COLS}
        regions, index = site_map.regions_of(files, rows, at)
        self.assertEqual([r["label"] for r in regions], ["Game Util", "Os Util"])
        self.assertEqual(index, [0, 1])

    def test_feedback_and_streamed_flags_come_from_the_run_reach(self) -> None:
        self.reach("p1", [1, 3])
        self.reach("p2", [0], stream=True)
        self.feedback("p1")
        rows = {row[0]: row[3] for row in self.rows()}
        self.assertTrue(rows["fn_b"] & BIT["feedback"] and rows["fn_d"] & BIT["feedback"])
        self.assertFalse(rows["fn_a"] & BIT["feedback"])
        self.assertTrue(rows["fn_a"] & BIT["streamed"])
        self.assertFalse(rows["fn_b"] & BIT["streamed"])

    def test_the_answer_is_deterministic_and_its_version_moves_with_the_rows(self) -> None:
        first = site_map.answer(self.server, "GET", "", "", b"")[1]
        again = site_map.answer(self.server, "GET", "", "", b"")[1]
        self.assertEqual(json.dumps(first), json.dumps(again))
        self.reach("p1", [0])
        self.feedback("p1")
        self.assertNotEqual(first["version"], site_map.answer(self.server, "GET", "", "", b"")[1]["version"])
        self.assertEqual(first["filters"], [{"key": k, "bit": b} for k, b in BIT.items()])

    def test_no_baked_site_answers_503_and_other_paths_404(self) -> None:
        (self.server.site / "manifest.json").unlink()
        self.assertEqual(site_map.answer(self.server, "GET", "", "", b"")[0], HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(site_map.answer(self.server, "GET", "x", "", b"")[0], HTTPStatus.NOT_FOUND)
        self.assertEqual(site_map.answer(self.server, "POST", "", "", b"")[0], HTTPStatus.NOT_FOUND)


SCRIPT = """
import * as m from %r;
const filters = %s;
const bit = Object.fromEntries(filters.map(f => [f.key, f.bit]));
const rows = [["a", 30, "named", bit.named], ["b", 10, "asm", 0], ["c", 5, "exact", bit.differs]];
const blocks = [{ start: 0, end: 2 }, { start: 2, end: 3 }];
const out = {
  none: m.isLit(0xffff, m.maskOf(filters, new Set())),
  named: m.isLit(bit.named | bit.behaves, m.maskOf(filters, new Set(["named"]))),
  other: m.isLit(bit.named, m.maskOf(filters, new Set(["asm", "differs"]))),
  colours: [
    m.colourKey("named", 0, bit),
    m.colourKey("exact", bit.behaves, bit),
    m.colourKey("asm", bit.behaves | bit.differs, bit),
    m.colourKey("zzz", 0, bit),
  ],
  cols: [m.gridCols(160), m.gridCols(1), m.gridCols(0)],
  light: [
    m.blockLight(rows, { start: 0, end: 2 }, bit, bit.named, false),
    m.blockLight(rows, { start: 0, end: 2 }, bit, 0, true),
    m.blockLight(rows, { start: 0, end: 2 }, bit, 0, false),
    m.blockLight(rows, { start: 2, end: 3 }, bit, bit.named, false),
  ],
  full: [
    m.fullyLit(rows, blocks, bit, 0, true),
    m.fullyLit(rows, blocks, bit, bit.named, false),
    m.fullyLit(rows, blocks, bit, 0, false),
  ],
  all: [m.allFrom("#/map?lit=all"), m.allFrom("#/map?lit=named"), m.allFrom("#/map")],
  lit: [
    m.litParam(filters, new Set(["differs", "named"])),
    [...m.litFrom("#/?lit=asm,zzz,exact", filters)],
    [...m.litFrom("#/fn/x", filters)],
  ],
};
console.log(JSON.stringify(out));
"""


class PageLogicTest(unittest.TestCase):
    """map.js's pure functions run under node against the contract's filters (the same rules as site_map)."""

    def test_filters_and_colours_in_the_page_match_the_server(self) -> None:
        filters = site_core.contract()["filters"]
        script = SCRIPT % ((ROOT / "static" / "site" / "map.js").as_uri(), json.dumps(filters))
        run = subprocess.run(
            ["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True, timeout=60
        )
        got = json.loads(run.stdout)
        self.assertFalse(got["none"])
        self.assertTrue(got["named"])
        self.assertFalse(got["other"])
        self.assertEqual(got["colours"], ["named", "behaves", "differs", "asm"])
        server = [
            site_map.colour_key("named", 0),
            site_map.colour_key("exact", bits("behaves")),
            site_map.colour_key("asm", bits("behaves", "differs")),
            site_map.colour_key("zzz", 0),
        ]
        self.assertEqual(got["colours"], server)
        self.assertEqual(got["cols"], [16, 2, 1])
        self.assertEqual(
            got["light"],
            [
                {"share": 0.75, "key": "named"},
                {"share": 1, "key": "named"},
                {"share": 0, "key": "asm"},
                {"share": 0, "key": "asm"},
            ],
        )
        self.assertEqual(got["full"], [True, False, False])
        self.assertEqual(got["all"], [True, False, False])
        self.assertEqual(got["lit"], ["named,differs", ["exact", "asm"], []])


if __name__ == "__main__":
    unittest.main()
