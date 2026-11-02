"""The play page (static/site/play.js) mounts on the real contract and sends input the server handler takes (card
SITE-PLAY-BOOT): a fresh #/play load crashed reading ctx.contract.feedback_categories before the contract arrived, and
the page heartbeat /api/input at a run not yet taken, ignoring each 400 and its named cause."""

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, site_core, tas, tas_serve

ROOT = Path(__file__).resolve().parents[1] / "src" / "abu_decomp"

# A DOM and window just wide enough for play.js; the script prints what the page sent and showed after each step.
HARNESS = """
class Node {
  constructor(tag) {
    this.tag = tag; this.kids = []; this.attrs = {}; this.on = {}; this.dataset = {}; this.textContent = "";
  }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "disabled") this.disabled = true; if (k === "id") this.id = v; }
  append(...k) { this.kids.push(...k); }
  replaceChildren(...k) { this.kids = k; }
  addEventListener(kind, fn) { (this.on[kind] ||= []).push(fn); }
  fire(kind, e = {}) { for (const fn of this.on[kind] || []) fn({ preventDefault() {}, ...e }); }
  all(out = []) { for (const k of this.kids) if (k instanceof Node) { out.push(k); k.all(out); } return out; }
  querySelector(sel) { return this.all().find(n => `#${n.id}` === sel) || null; }
  get elements() {
    const picked = this.all().find(n => n.attrs.name === "category" && "checked" in n.attrs);
    return { category: { value: picked?.attrs.value } };
  }
  showModal() { this.open = true; } close() { this.open = false; }
}
const win = {}, frames = [];
let now = 0;
globalThis.document = { createElement: t => new Node(t), getElementById: () => null, head: new Node("head") };
globalThis.addEventListener = (kind, fn) => { (win[kind] ||= []).push(fn); };
globalThis.removeEventListener = () => {};
globalThis.requestAnimationFrame = fn => { frames.push(fn); return frames.length; };
globalThis.cancelAnimationFrame = () => {};
globalThis.location = { hash: "#/play" };
globalThis.history = { replaceState: (a, b, url) => { location.hash = url; } };
globalThis.fetch = async () => ({ ok: false });
Object.defineProperty(globalThis, "performance", { value: { now: () => now } });
Object.defineProperty(globalThis, "navigator", { value: { getGamepads: () => [] } });

const [contract, event, answers] = %s;
const sent = [], listeners = {};
const ctx = {
  contract, route: { name: "play", arg: "", query: {} },
  on: (kind, fn) => { (listeners[kind] ||= []).push(fn); },
  api: async (path, init) => {
    if (init) sent.push({ path, body: JSON.parse(init.body) });
    if (path === "/api/play") return { id: event.fields.run, movie: event.fields.movie };
    if (path === "/api/input") return answers.length > 1 ? answers.shift() : answers[0];
    return path === "/api/feed" ? { events: [] } : { feedback: [] };
  },
};
// One event-loop turn: every promise the page chained on a fake api answer resolves before the next step (no clock).
const settle = () => new Promise(ok => setImmediate(ok));
const key = async code => {
  for (const kind of ["keydown", "keyup"]) {
    for (const fn of win[kind]) fn({ code, preventDefault() {} });
    await settle();
  }
};
const tick = () => { now += 600; const fn = frames.pop(); frames.length = 0; fn(); };
const inputs = () => sent.filter(s => s.path === "/api/input").map(s => s.body);
const play = await import(%s);
const el = new Node("main");
play.mount(el, ctx);
await settle();
const tag = el.querySelector("#play-run"), out = {};
out.ready = el.dataset.ready;
out.radios = el.all().filter(n => n.attrs.type === "radio").map(n => n.attrs.value);
el.querySelector("#play-start").fire("click");
await settle();
await key("KeyX"); tick(); tick(); await settle();
out.before_live = inputs().length;
for (const fn of listeners[event.kind]) fn(event);
await key("KeyX");
out.first = inputs();
await key("KeyX");
for (let i = 0; i < 5; i++) tick();
await settle();
out.after_refusal = inputs().length;
out.tag = tag.textContent;
console.log(JSON.stringify(out));
"""


class FakeServer:
    """The handler's server with the real input path: Server.input over a run folder this test lays out."""

    input = tas_serve.Server.input

    def __init__(self, runs: Path) -> None:
        self.feed = site_core.Feed()
        self.runs, self.sent = runs, []

    def run_dir(self, run_id: str) -> Path | None:
        run = self.runs / run_id
        return run if run.is_dir() else None

    def send(self, run: Path, data: bytes) -> None:
        if not (run / "input.sock").exists():
            raise DecompError("tas.input", "the run's input socket is not there yet")
        self.sent.append((run.name, data))


class PlayPageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name)
        self.server = FakeServer(self.runs)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), tas_serve.handler(self.server))  # type: ignore[arg-type]
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    def tearDown(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.tmp.cleanup()

    def call(self, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as reply:
                return reply.status, json.loads(reply.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def lay_out(self, run_id: str, taken: bool) -> None:
        run = self.runs / run_id
        run.mkdir()
        (run / tas.RECORD).write_text(json.dumps({"id": run_id, "pads": 1}))
        (run / "input.sock").write_text("")
        if taken:
            (run / tas.TAKEN).write_text("new run\n")

    def page(self, answers: list[dict[str, Any]]) -> dict[str, Any]:
        """play.js mounted on the contract the handler serves, driven by the feed event a live human run publishes."""
        status, contract = self.call("/api/contract")
        self.assertEqual(status, 200)
        row = {"id": "r9", "status": "running", "input": "human", "poll": 40, "polls": 0, "started": 1.0}
        (event,) = site_core.run_events(site_core.run_record(row))
        script = HARNESS % (json.dumps([contract, event, answers]), json.dumps((ROOT / "static/site/play.js").as_uri()))
        run = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_play_mounts_on_the_real_contract_with_one_radio_per_feedback_category(self):
        got = self.page([{"ok": True}] * 4)
        self.assertEqual(got["ready"], "play")
        self.assertEqual(got["radios"], list(site_core.contract()["feedback_categories"]))

    def test_the_input_the_page_sends_once_the_run_is_live_is_taken_by_the_server_handler(self):
        self.lay_out("r9", taken=True)
        got = self.page([{"ok": True}] * 4)
        self.assertEqual(got["before_live"], 0, "no input before the run's feed event says live input drives it")
        body = got["first"][0]
        self.assertEqual(body, {"id": "r9", "seq": 1, "buttons": 0x8000, "x": 0, "y": 0, "pad": 0})
        self.assertEqual(self.call("/api/input", body), (200, {"ok": True}))
        self.assertEqual(self.server.sent, [("r9", b"1 8000 0 0")])

    def test_a_refused_input_names_its_cause_the_page_shows_it_and_stops_sending(self):
        self.lay_out("r9", taken=False)
        status, refusal = self.call("/api/input", {"id": "r9", "seq": 1, "buttons": 0x8000, "x": 0, "y": 0, "pad": 0})
        self.assertEqual((status, refusal), (400, {"error": "run r9 is not driven by live input"}))
        got = self.page([refusal, refusal, refusal, refusal])
        self.assertIn("input refused: run r9 is not driven by live input", got["tag"])
        # one refused key press, then one retry on the next press: heartbeats never resend into a refusal
        self.assertEqual(got["after_refusal"], 2)


class ShellTest(unittest.TestCase):
    def test_the_shell_mounts_a_region_only_after_the_contract_and_refuses_visibly_without_it(self):
        page = (ROOT / "runner.html").read_text()
        show = page[page.index("async function show()") : page.index("// The contract, fetched once")]
        self.assertLess(show.index("contract()"), show.index("mod.mount(view, ctx)"))
        self.assertIn('id: "contract-refused"', show)
        self.assertNotIn('r.name === "map" && !S.contract', page)


if __name__ == "__main__":
    unittest.main()
