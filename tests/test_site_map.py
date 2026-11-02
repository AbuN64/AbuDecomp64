"""The Runner site map sidebar: region groups and the two views' steps (site_map), the modal route (map.js)."""

import json
import shutil
import subprocess
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from abu_decomp import site_map

ROOT = Path(__file__).resolve().parents[1] / "src" / "abu_decomp"
COLS = ["name", "rom", "size", "state", "file", "reach", "foundation", "behaviour"]
FILES = ["src/game/LEVEL/a.CPP", "src/game/MENU_SCREEN/b.CPP", "src/os/c.CPP"]
ROWS = [
    ["fn_a", 0, 64, "named", 0, "both", 0, "behaves"],
    ["fn_b", 100, 32, "exact", 1, "port", 0, "differs-args"],
    ["fn_c", 200, 16, "asm", 0, "oracle", 0, ""],
    ["fn_d", 300, 8, "fuzzy", 2, "none", 0, "missing"],
]


def region(label: str, count: int, rom: int) -> dict:
    return {"key": label, "label": label, "count": count, "bytes": count * 10, "rom": rom}


class StepTest(unittest.TestCase):
    def test_decomp_runs_assembly_then_unmatched_c_then_matching_c(self) -> None:
        got = [site_map.decomp_step(s) for s in ("asm", "fuzzy", "exact", "named", "zzz")]
        self.assertEqual(got, [0, 1, 2, 2, 0])

    def test_behaviour_runs_unexercised_then_exercised_not_exact_then_exact(self) -> None:
        self.assertEqual(site_map.behaviour_step("none", ""), 0)
        self.assertEqual(site_map.behaviour_step("oracle", ""), 0)  # only the original ran it: the port never did
        self.assertEqual(site_map.behaviour_step("port", ""), 1)  # exercised, not yet proven equal
        self.assertEqual(site_map.behaviour_step("both", "differs-args"), 1)
        self.assertEqual(site_map.behaviour_step("none", "differs-ret"), 1)
        self.assertEqual(site_map.behaviour_step("both", "behaves"), 2)

    def test_every_view_has_three_steps(self) -> None:
        self.assertEqual([v["key"] for v in site_map.VIEWS], ["decomp", "behaviour"])
        self.assertTrue(all(len(v["steps"]) == 3 for v in site_map.VIEWS))


class GroupTest(unittest.TestCase):
    def test_few_regions_stay_one_group_each(self) -> None:
        groups, of = site_map.groups_of([region("A", 5, 0), region("B", 1, 10)])
        self.assertEqual([g["label"] for g in groups], ["A", "B"])
        self.assertEqual(of, [0, 1])

    def test_many_regions_merge_rom_adjacent_smallest_pair_first_to_the_target(self) -> None:
        regions = [region(c, n, i * 100) for i, (c, n) in enumerate(zip("ABCDEF", (50, 1, 2, 40, 3, 30), strict=True))]
        groups, of = site_map.groups_of(regions, target=3)
        self.assertEqual(len(groups), 3)
        self.assertEqual([g["members"] for g in groups], [["A"], ["B", "C", "D"], ["E", "F"]])
        self.assertEqual([g["label"] for g in groups], ["A", "D", "F"])  # labelled by the largest member
        self.assertEqual([g["count"] for g in groups], [50, 43, 33])
        self.assertEqual(of, [0, 1, 1, 1, 2, 2])
        self.assertEqual(sum(g["count"] for g in groups), sum(r["count"] for r in regions))

    def test_default_target_is_about_a_dozen(self) -> None:
        groups, _ = site_map.groups_of([region(f"R{i}", i + 1, i) for i in range(40)])
        self.assertEqual(len(groups), site_map.GROUPS)
        self.assertEqual(site_map.GROUPS, 12)


class BuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        site = Path(self.tmp.name) / "site"
        site.mkdir()
        (site / "fns.abc.json").write_text(json.dumps({"ref": "r1", "files": FILES, "cols": COLS, "rows": ROWS}))
        (site / "manifest.json").write_text(json.dumps({"fns": "fns.abc.json"}))
        self.server = SimpleNamespace(cfg={"runs": Path(self.tmp.name)}, site=site)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def answer(self) -> dict:
        status, body = site_map.answer(self.server, "GET", "", "", b"")
        self.assertEqual(status, HTTPStatus.OK)
        return body

    def test_rows_are_name_size_decomp_behaviour_group_in_rom_order(self) -> None:
        body = self.answer()
        self.assertEqual(
            body["rows"],
            [["fn_a", 64, 2, 2, 0], ["fn_b", 32, 2, 1, 1], ["fn_c", 16, 0, 0, 0], ["fn_d", 8, 1, 0, 2]],
        )
        self.assertEqual([g["label"] for g in body["groups"]], ["Level", "Menu Screen", "Os"])
        self.assertEqual(body["views"], list(site_map.VIEWS))
        self.assertEqual(body["ref"], "r1")

    def test_the_answer_is_deterministic_and_its_version_moves_with_the_rows(self) -> None:
        first = self.answer()
        self.assertEqual(first, self.answer())
        rows = [*ROWS[:3], ["fn_d", 300, 8, "exact", 2, "none", 0, "missing"]]
        (self.server.site / "fns.abc.json").write_text(json.dumps({"files": FILES, "cols": COLS, "rows": rows}))
        self.assertNotEqual(first["version"], self.answer()["version"])

    def test_no_baked_site_answers_503_and_other_paths_404(self) -> None:
        (self.server.site / "manifest.json").unlink()
        self.assertEqual(site_map.answer(self.server, "GET", "", "", b"")[0], HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(site_map.answer(self.server, "GET", "x", "", b"")[0], HTTPStatus.NOT_FOUND)
        self.assertEqual(site_map.answer(self.server, "POST", "", "", b"")[0], HTTPStatus.NOT_FOUND)


SCRIPT = """
import * as m from %r;
const views = %s;
const rows = [["a", 1, 2, 0, 0], ["b", 1, 0, 0, 1], ["c", 1, 1, 1, 0]];
console.log(JSON.stringify({
  open: m.withFn("#/stream/7", "fn a"),
  keep: m.withFn("#/play?x=1", "f"),
  close: [m.withFn("#/stream/7?fn=x", null), m.withFn("#/?fn=x&x=1", null), m.withFn("", null)],
  parse: m.parseHash("#/stream/7?fn=fn%%20a"),
  key: [m.routeKey("#/stream/7?fn=x"), m.routeKey("#/stream/7"), m.routeKey("")],
  view: [m.viewFor("play", "decomp", views), m.viewFor("", "behaviour", views), m.viewFor("", "zzz", views)],
  bucket: m.bucket(rows, [{}, {}]),
  tally: [m.tally(rows, "decomp"), m.tally(rows, "behaviour")],
}));
"""


@unittest.skipUnless(shutil.which("node"), "node runs the sidebar module's pure logic")
class PageLogicTest(unittest.TestCase):
    def test_modal_route_view_lock_and_grid_buckets(self) -> None:
        script = SCRIPT % (str(ROOT / "static" / "site" / "map.js"), json.dumps(list(site_map.VIEWS)))
        run = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
        got = json.loads(run.stdout)
        self.assertEqual(got["open"], "#/stream/7?fn=fn+a")
        self.assertEqual(got["keep"], "#/play?x=1&fn=f")
        self.assertEqual(got["close"], ["#/stream/7", "#/?x=1", "#/"])
        self.assertEqual(got["parse"], {"path": "stream/7", "query": {"fn": "fn a"}})
        self.assertEqual(got["key"], ["stream/7", "stream/7", ""])  # the modal never changes the mounted route
        self.assertEqual(got["view"], ["behaviour", "behaviour", "decomp"])  # play shows the behaviour grid only
        self.assertEqual(got["bucket"], [[0, 2], [1]])
        self.assertEqual(got["tally"], [[1, 1, 1], [2, 1, 0]])


class PageTest(unittest.TestCase):
    def test_the_shell_mounts_the_sidebar_and_has_no_map_or_fn_route(self) -> None:
        page = (ROOT / "runner.html").read_text()
        self.assertIn('<aside id="sitemap"', page)
        self.assertIn('const REGION = { stream: "stream", play: "play" }', page)
        self.assertNotIn("#/map", page)
        self.assertNotIn("#/fn/", page)
        self.assertNotIn(
            "animation", (ROOT / "static" / "site" / "site.css").read_text()
        )  # still: no decorative motion
