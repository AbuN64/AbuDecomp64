"""The play page: the owner's session-scoped Behaviour grid (map.js sessionReach/sessionRows fed by play.js reachRows
from the reach records interleaved in /video) and the controls blurb (play.js controls)."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import unittest
from pathlib import Path

from abu_decomp import site_map

ROOT = Path(__file__).resolve().parents[1] / "src" / "abu_decomp"
SITE = ROOT / "static" / "site"
REACH_FLAG = 0x80000000  # REACH_FLAG: a /video body that is a reach record


def leb(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 127, n >> 7
        out.append(byte | (128 if n else 0))
        if not n:
            return bytes(out)


def delta(rows: list[int]) -> bytes:
    out, prev = leb(len(rows)), 0
    for row in rows:
        out, prev = out + leb(row - prev), row
    return out


def frame(body: bytes, reach: bool, stamp: int = 0) -> bytes:
    return struct.pack(">IQI", len(body) | (REACH_FLAG if reach else 0), stamp, 0) + body


SCRIPT = """
import * as play from %r;
import * as map from %r;
const hex = s => Uint8Array.from(s.match(/../g) || [], b => parseInt(b, 16));
const recs = %s, stream = hex(%r);
const rows = [["a", 1, 2, 0, 0], ["b", 1, 0, 2, 1], ["c", 1, 1, 1, 0], ["d", 1, 0, 0, 1]];
const out = {
  empty: map.tally(map.sessionRows(rows, new Set()), "behaviour"),
  decoded: recs.map(r => play.reachRows(hex(r))),
};
const frames = [], reaches = [];
const feed = play.framer((f, stamp) => frames.push([f.length, stamp]), r => reaches.push(play.reachRows(r)));
feed(stream.slice(0, 7)); feed(stream.slice(7));
out.frames = frames; out.reaches = reaches;
const seen = new Set();
const grew = [map.sessionReach([2]), map.sessionReach([2]), map.sessionReach([1, 3])];
out.grew = grew;
// the module's own session, read back through a fresh Set of the rows it reported
for (const r of [2, 1, 3]) seen.add(r);
out.after = map.sessionRows(rows, seen).map(r => r[3]);
out.tally = map.tally(map.sessionRows(rows, seen), "behaviour");
out.scoped = map.SESSION;
out.controls = play.controls();
// the painter: frames between two animation frames collapse to the newest stamp; an older stamp never repaints
const ticks = [], painted = [];
const show = play.painter(b => { painted.push(b); }, fn => ticks.push(fn));
const tick = async () => { ticks.shift()(); await Promise.resolve(); await Promise.resolve(); };
show.offer("a", 10); show.offer("b", 20); show.offer("old", 15); await tick();
show.offer("stale", 5); await tick();
show.offer("c", 30); await tick(); await tick();
show.stop(); show.offer("d", 40); await tick();
out.painted = painted; out.shown = show.shown;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node runs the play and sidebar modules' pure logic")
class SessionGridTest(unittest.TestCase):
    def setUp(self) -> None:
        full = struct.pack("<BI", 1, 9) + bytes([0b00000101, 0b10000000])  # rows 0, 2, 15
        step = struct.pack("<BI", 2, 10) + delta([3, 40]) + delta([1, 200])  # fresh 3, 40; hot 1, 200
        cut = struct.pack("<BI", 2, 10) + leb(2) + leb(3)  # a count of 2 with one row: cut off
        trail = step + b"\x00"
        jpeg = b"\xff\xd8jpeg\xff\xd9"
        stream = frame(jpeg, False, 7) + frame(step, True) + frame(jpeg, False, 2**40)
        recs = json.dumps([r.hex() for r in (full, step, cut, trail, b"\x03abcd")])
        script = SCRIPT % (str(SITE / "play.js"), str(SITE / "map.js"), recs, stream.hex())
        run = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
        self.got = json.loads(run.stdout)

    def test_reach_records_decode_to_rows_and_bad_records_name_none(self) -> None:
        self.assertEqual(self.got["decoded"], [[0, 2, 15], [3, 40, 1, 200], [], [], []])

    def test_framer_splits_pictures_from_reach_records_across_chunks(self) -> None:
        self.assertEqual(self.got["frames"], [[8, 7], [8, 2**40]])  # each picture with its capture stamp
        self.assertEqual(self.got["reaches"], [[3, 40, 1, 200]])

    def test_painter_paints_the_newest_stamp_once_per_animation_frame(self) -> None:
        self.assertEqual(self.got["painted"], ["b", "c"])
        self.assertEqual(self.got["shown"], 30)

    def test_session_grid_starts_empty_and_fills_with_what_the_session_reached(self) -> None:
        self.assertEqual(self.got["empty"], [4, 0, 0])  # every square "Not exercised" before the first record
        self.assertEqual(self.got["grew"], [1, 0, 2])  # a row already reached adds nothing
        # reached rows show their proven step, at least "Exercised, differs";
        # unreached stay 0 even when proven elsewhere
        self.assertEqual(self.got["after"], [0, 2, 1, 1])
        self.assertEqual(self.got["tally"], [1, 2, 1])
        self.assertEqual(self.got["scoped"], {"play": True})

    def test_controls_blurb_lists_every_mapping_padstate_reads(self) -> None:
        table = {n64: (key, pad) for n64, key, pad in self.got["controls"]}
        self.assertEqual(table["A"], ("X", "A (bottom)"))
        self.assertEqual(table["B"], ("C", "B (right) / X (left)"))
        self.assertEqual(table["Z"], ("Z", "LT / RT"))
        self.assertEqual(table["Start"], ("Enter", "Start"))
        self.assertEqual(table["Stick up"], ("Up", "Left stick"))
        self.assertEqual(table["C up"], ("I", "Y (top)"))
        self.assertEqual(table["C down"], ("K", ""))
        self.assertEqual(len(table), 18)


class PlayPageTest(unittest.TestCase):
    def test_play_page_holds_the_controls_blurb_beside_the_game_and_the_grid_is_session_scoped(self) -> None:
        page = (SITE / "play.js").read_text()
        self.assertIn('import { sessionReach } from "./map.js"', page)
        self.assertIn('node("div", { class: "playarea" }, canvas, blurb)', page)
        self.assertIn("sessionReach(reachRows(rec))", page)
        self.assertEqual(site_map.VIEWS[1]["key"], "behaviour")
        self.assertIn('LOCKED = { play: "behaviour" }', (SITE / "map.js").read_text())

    def test_phone_width_puts_the_grid_above_the_game_without_animation(self) -> None:
        css = (SITE / "site.css").read_text()
        phone = css[css.index("@media (max-width: 760px)") :]
        self.assertIn(".frame, body.page .frame { display: block;", phone)
        self.assertNotIn("animation", (SITE / "play.js").read_text())
        self.assertNotIn("transition", (SITE / "play.js").read_text())


if __name__ == "__main__":
    unittest.main()
