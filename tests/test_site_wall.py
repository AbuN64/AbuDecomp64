"""The home wall's paging and the play events the feed lists (site_core.wall_page, function_events; wall.js logic)."""

import json
import shutil
import subprocess
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from abu_decomp import site_core

WALL_JS = Path(site_core.__file__).with_name("static") / "site" / "wall.js"


def row(ident: str, started: float, **extra) -> dict:
    return {
        "id": ident,
        "movie": "m",
        "status": "done",
        "outcome": "ok",
        "started": started,
        "ended": started + 1,
    } | extra


class WallPageTest(unittest.TestCase):
    def runs(self, n: int) -> list[dict]:
        return [site_core.run_record(row(f"r{i}", float(i), status="running" if i == 2 else "done")) for i in range(n)]

    def test_running_first_then_newest_and_pages_chain_to_the_end(self):
        runs = self.runs(30)
        first = site_core.wall_page(runs, 0, 12)
        self.assertEqual([t["id"] for t in first["tiles"][:3]], ["r2", "r29", "r28"])
        self.assertEqual((first["total"], first["next"]), (30, 12))
        seen, offset = [], 0
        while offset is not None:
            page = site_core.wall_page(runs, offset, 12)
            seen += [t["id"] for t in page["tiles"]]
            offset = page["next"]
        self.assertEqual(sorted(seen), sorted(r["id"] for r in runs))
        self.assertEqual(len(seen), len(set(seen)))

    def test_bad_page_is_refused_and_the_endpoint_answers_400(self):
        with self.assertRaises(ValueError):
            site_core.wall_page([], -1, 12)
        feed = site_core.Feed()
        feed.publish_runs([row("a", 1.0), row("b", 2.0)])
        server = SimpleNamespace(feed=feed)
        status, page = site_core.answer(server, "GET", "/api/wall", "offset=0&limit=1", b"")
        self.assertEqual((status, [t["id"] for t in page["tiles"]], page["next"]), (HTTPStatus.OK, ["b"], 1))
        status, page = site_core.answer(server, "GET", "/api/wall", "limit=0", b"")
        self.assertEqual((status, page["code"]), (HTTPStatus.BAD_REQUEST, "wall.page"))


class PlayEventsTest(unittest.TestCase):
    def kinds(self, feed: site_core.Feed) -> list[str]:
        return sorted(item["kind"] for item in feed.newest())

    def test_started_and_ended_are_both_listed(self):
        feed = site_core.Feed()
        feed.publish_runs([row("p", 5.0, status="running", poll=10, polls=100)])
        feed.publish_runs([row("p", 5.0, poll=100, polls=100)])
        self.assertEqual(self.kinds(feed), ["run.ended", "run.started:tas"])

    def test_mismatch_then_newly_exact_once(self):
        feed = site_core.Feed()
        unit = {"kind": "unit", "target": "Fn_A", "cases": 50}
        feed.publish_runs([row("u1", 1.0, equal=41, **unit)])
        self.assertIn("function.mismatch", self.kinds(feed))
        mismatch = next(i for i in feed.newest() if i["kind"] == "function.mismatch")
        self.assertEqual(site_core.render(mismatch), "runner detected a behaviour mismatch in Fn_A: 41/50 cases equal")
        feed.publish_runs([row("u1", 1.0, equal=41, **unit), row("u2", 2.0, equal=50, **unit)])
        matched = [i for i in feed.newest() if i["kind"] == "function.matched"]
        self.assertEqual([site_core.render(i) for i in matched], ["runner made function Fn_A newly exact"])
        # a later exact run of the same function is not news; the event keeps its first moment
        self.assertEqual(feed.publish_runs([row("u3", 3.0, equal=50, **unit)]), 1)  # only u3's run.ended
        self.assertEqual([i["at"] for i in feed.newest() if i["kind"] == "function.matched"], [3.0])

    def test_a_running_exact_run_is_not_yet_exact_and_tas_runs_name_no_function(self):
        exact: set[str] = set()
        running = site_core.run_record(row("u", 1.0, status="running", kind="unit", target="F", equal=5, cases=5))
        self.assertEqual(site_core.function_events(running, exact), [])
        self.assertEqual(site_core.function_events(site_core.run_record(row("t", 1.0)), exact), [])


@unittest.skipUnless(shutil.which("node"), "node runs the wall module")
class WallJsTest(unittest.TestCase):
    def test_paging_merge_frames_and_streams_equal_visible_tiles(self):
        script = f"""
import {{ merge, frameUrl, mount }} from {json.dumps(WALL_JS.as_uri())};
const out = {{ merge: merge(["a", "b"], [{{id: "b"}}, {{id: "c"}}]),
  frame: frameUrl("r 1", {{polls: [0, 60, 120]}}), none: frameUrl("r", {{polls: []}}) }};
class Node {{
  constructor() {{ this.dataset = {{}}; this.kids = []; this.isConnected = true; }}
  append(...k) {{ this.kids.push(...k); }}
  prepend(...k) {{ this.kids.unshift(...k); }}
  replaceChildren(...k) {{ this.kids = k; }}
}}
const doc = {{ createElement: () => new Node() }};
const observers = [];
class Obs {{
  constructor(fn) {{ this.fn = fn; this.els = []; observers.push(this); }}
  observe(e) {{ this.els.push(e); }}
}}
const tiles = Array.from({{length: 30}}, (_, i) =>
  ({{ id: "r" + i, lane: "L", movie: "m", status: "running", poll: i, polls: 9, sides: ["port"] }}));
const api = async path => {{
  if (path.startsWith("/api/wall")) {{
    const q = new URLSearchParams(path.split("?")[1]);
    const o = +q.get("offset"), l = +q.get("limit");
    return {{ total: 30, offset: o, tiles: tiles.slice(o, o + l), next: o + l < 30 ? o + l : null }};
  }}
  return {{ polls: [0, 60] }};
}};
const host = new Node();
await mount(host, {{ api, on: () => {{}}, doc, Observer: Obs }});
const [grid, more] = host.kids, tileObs = observers[0], moreObs = observers[1];
moreObs.fn([{{ isIntersecting: true, target: more }}]); await new Promise(r => setTimeout(r, 0));
const shown = grid.kids.slice(0, 6);
tileObs.fn(shown.map(t => ({{ isIntersecting: true, target: t }}))); await new Promise(r => setTimeout(r, 0));
out.before = grid.dataset.streams;  // requested, no frame loaded yet: nothing streams
shown.slice(0, 5).forEach(t => t.kids[0].onload()); shown[5].kids[0].onerror();  // five frames load, one fails
tileObs.fn(shown.slice(0, 2).map(t => ({{ isIntersecting: false, target: t }})));
out.tiles = grid.kids.length; out.visible = grid.dataset.visible; out.streams = grid.dataset.streams;
out.src = shown[3].kids[0].src; out.offscreenSrc = shown[0].kids[0].src;
console.log(JSON.stringify(out));
"""
        run = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
        got = json.loads(run.stdout)
        self.assertEqual(got["merge"], ["a", "b", "c"])
        self.assertEqual((got["frame"], got["none"]), ("/stream/r%201/port/120.jpg", None))
        self.assertEqual(got["tiles"], 24)  # two pages: the first on mount, the second when the sentinel shows
        self.assertEqual(got["before"], 0)  # a request alone is not a stream
        self.assertEqual((got["visible"], got["streams"]), (4, 3))  # streams: visible tiles whose frame loaded
        self.assertEqual(got["src"], "/stream/r3/port/60.jpg")
        self.assertEqual(got["offscreenSrc"], "/stream/r0/port/60.jpg")  # an off-screen tile keeps its still frame


if __name__ == "__main__":
    unittest.main()
