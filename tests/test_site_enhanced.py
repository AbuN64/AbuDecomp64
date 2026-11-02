"""The Enhanced progress page's record (module site_enhanced) and the pure logic of static/site/enhanced.js. The
ButlerMaster seam (`eta_rows`) is mocked: no board is read."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import DecompError, site_core, site_enhanced

JS = Path(site_enhanced.__file__).parent / "static" / "site" / "enhanced.js"
SPLIT = """# plan

| key | scope | est_min | repositories | waits |
| --- | --- | --- | --- | --- |
| EXIT-WIRE | wire quit | 15 | RWE | none |
| SEAM-BASE | base line | 10 | ADE | none |
| SEAM-PERSIST | persist | 20 | ADE | SEAM-BASE |
| EXIT-UNIT | unit | 5 | RWE | EXIT-WIRE |
| LINUX-PROOF | proof | 10 | RWE | EXIT-UNIT, SEAM-PERSIST |
| LAUNCH-GATE | gate | 5 | RWE | LINUX-PROOF |
"""
MVP = """| Item | Player-visible acceptance | Card (state) | Split rows |
| --- | --- | --- | --- |
| Clean quit | Quit exits 0. | EXIT old-card (NotReady) | EXIT-WIRE, EXIT-UNIT |
| One settings file | One cfg. | SEAM old-seam (NotReady) | SEAM-BASE, SEAM-PERSIST |
| Linux public-runtime proof | Linux launches. | EXIT and the above | LINUX-PROOF |

Not in MVP: nothing.
"""
MADE = {
    "EXIT-WIRE": "card-wire",
    "SEAM-BASE": "card-base",
    "SEAM-PERSIST": "card-persist",
    "EXIT-UNIT": "card-unit",
    "LINUX-PROOF": "card-proof",
    "LAUNCH-GATE": "card-gate",
}
ROWS = {
    "card-persist": {"Step": 2, "Status": "Step 2 of 4: the bind table is read", "Eta": "~5 min"},
    "card-unit": {"Step": 0, "Eta": "no step finished", "WaitsOn": "EXIT-WIRE"},
    "card-proof": {"Step": 0, "Eta": "no step finished"},
    "card-gate": {"Step": 0, "Eta": "~9 min"},
}
BUILD = {"port": "/p/port", "pin": "a" * 40, "built": 1.5, "format": 3}


class Planned(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / site_enhanced.SPLIT_FILE).write_text(SPLIT)
        (root / site_enhanced.MVP_FILE).write_text(MVP)
        (root / site_enhanced.MADE_FILE).write_text(json.dumps(MADE))
        self.config = {
            "butlermaster": "http://butler.example.lan:1",
            "board": "board-1",
            "kan": "http://kan.example.lan",
            "plan": str(root),
        }
        self.edge = SimpleNamespace(enhanced=self.config, state=lambda: {"build": BUILD, "runs": []})

    def get(self, rows=ROWS):
        with mock.patch.object(site_enhanced, "eta_rows", return_value=rows) as eta:
            result = site_enhanced.answer(self.edge, "GET", "", "", b"")
        if result[0] == HTTPStatus.OK:
            eta.assert_called_once_with("http://butler.example.lan:1", "board-1")
        return result


class Record(Planned):
    def test_the_items_come_from_the_plan_with_their_rows_and_states(self):
        status, got = self.get()
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual([item["name"] for item in got["items"]], ["Clean quit", "One settings file"])
        quit_item, seam_item = got["items"]
        self.assertEqual(
            [(r["key"], r["card"], r["state"]) for r in quit_item["rows"]],
            [("EXIT-WIRE", "card-wire", "closed"), ("EXIT-UNIT", "card-unit", "open")],
        )
        self.assertEqual(quit_item["state"], "working")  # one row closed, one open
        self.assertEqual(quit_item["rows"][1]["line"], "waits on EXIT-WIRE")
        self.assertEqual(seam_item["state"], "working")
        self.assertEqual(seam_item["rows"][1]["line"], "Step 2 of 4: the bind table is read")
        self.assertEqual(got["plan"], {"closed": 2, "working": 1, "open": 3})
        self.assertEqual(got["kan"], "http://kan.example.lan")
        self.assertEqual(got["build"], {**BUILD, "play": "#/play"})

    def test_a_closed_card_closes_its_item_and_old_cards_do_not_appear(self):
        _, got = self.get({})
        self.assertEqual({item["state"] for item in got["items"]}, {"closed"})
        self.assertNotIn("old-card", json.dumps(got))
        self.assertNotIn("old-seam", json.dumps(got))

    def test_the_critical_path_is_the_longest_chain_and_its_rows_are_marked(self):
        _, got = self.get()
        self.assertEqual(
            [r["key"] for r in got["critical"]["rows"]], ["SEAM-BASE", "SEAM-PERSIST", "LINUX-PROOF", "LAUNCH-GATE"]
        )
        self.assertEqual(got["critical"]["minutes"], 45)
        marked = {r["key"] for item in got["items"] for r in item["rows"] if r["critical"]}
        self.assertEqual(marked, {"SEAM-BASE", "SEAM-PERSIST"})

    def test_the_finish_line_is_the_platform_proofs_and_the_gate(self):
        _, got = self.get()
        self.assertEqual([item["name"] for item in got["finish"]["proofs"]], ["Linux public-runtime proof"])
        self.assertEqual([(r["key"], r["card"]) for r in got["finish"]["gate"]], [("LAUNCH-GATE", "card-gate")])

    def test_a_row_without_a_card_is_refused_not_guessed(self):
        (Path(self.config["plan"]) / site_enhanced.MADE_FILE).write_text(json.dumps({"EXIT-WIRE": "c"}))
        status, got = self.get()
        self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
        self.assertIn("SEAM-BASE has no card", got["error"])

    def test_a_waits_cycle_is_refused(self):
        (Path(self.config["plan"]) / site_enhanced.SPLIT_FILE).write_text(
            SPLIT.replace(
                "| EXIT-WIRE | wire quit | 15 | RWE | none |", "| EXIT-WIRE | wire quit | 15 | RWE | EXIT-UNIT |"
            )
        )
        status, got = self.get()
        self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
        self.assertIn("cycle", got["error"])

    def test_butlermaster_refusing_shows_its_cause(self):
        with mock.patch.object(site_enhanced, "eta_rows", side_effect=DecompError("site.enhanced", "unreachable")):
            status, got = site_enhanced.answer(self.edge, "GET", "", "", b"")
        self.assertEqual((status, got), (HTTPStatus.BAD_GATEWAY, {"error": "unreachable"}))

    def test_an_unset_profile_section_answers_501_and_other_paths_refuse(self):
        self.edge.enhanced = None
        self.assertEqual(site_enhanced.answer(self.edge, "GET", "", "", b"")[0], HTTPStatus.NOT_IMPLEMENTED)
        self.assertEqual(site_enhanced.answer(self.edge, "POST", "", "", b"")[0], HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(site_enhanced.answer(self.edge, "GET", "x", "", b"")[0], HTTPStatus.BAD_REQUEST)

    def test_the_region_is_routed_through_the_held_snapshot_not_a_read(self):
        held = site_core.Snapshots(site_core.Feed())
        self.edge.snapshots = held
        held.register("enhanced", lambda: site_enhanced.answer(self.edge, "GET", "", "", b""))
        self.assertEqual(
            site_core.answer(self.edge, "GET", "/api/enhanced", "", b"")[0], HTTPStatus.SERVICE_UNAVAILABLE
        )
        with mock.patch.object(site_enhanced, "eta_rows", return_value=ROWS) as eta:
            held.refresh("enhanced")
            for _ in range(3):
                status, got = site_core.answer(self.edge, "GET", "/api/enhanced", "", b"")
        self.assertEqual((status, got["plan"]["working"]), (HTTPStatus.OK, 1))
        eta.assert_called_once()  # three requests, one ButlerMaster read

    def test_a_failing_source_holds_its_error_and_the_others_keep_theirs(self):
        held = site_core.Snapshots(site_core.Feed())
        held.register("map", lambda: (HTTPStatus.OK, {"rows": []}))
        held.register("enhanced", lambda: 1 / 0)
        held.refresh("enhanced")
        held.refresh("map")
        self.assertEqual(held.get("map")[0], HTTPStatus.OK)
        self.assertEqual(held.get("enhanced")[0], HTTPStatus.INTERNAL_SERVER_ERROR)

    def test_a_feed_change_rebuilds_the_snapshot(self):
        feed = site_core.Feed()
        held = site_core.Snapshots(feed)
        built = []
        held.register("map", lambda: (HTTPStatus.OK, built.append(1) or len(built)))
        held.refresh("map")
        version = feed.version
        feed.publish([site_core.event("run.queued", 1.0, "l", "r", movie="m")])
        self.assertGreater(feed.version, version)
        self.assertEqual(feed.since(version)[0], feed.version)  # a stream parked at `version` wakes at once


class Settings(unittest.TestCase):
    def test_settings_are_validated(self):
        good = {"butlermaster": "http://b", "board": "b", "kan": "http://k", "plan": "/p"}
        self.assertIsNone(site_enhanced.settings(None))
        self.assertEqual(site_enhanced.settings(dict(good)), good)
        lacking = {k: v for k, v in good.items() if k != "board"}
        for bad in ({**good, "items": {}}, {**good, "story": "s"}, lacking, {**good, "plan": 1}):
            with self.assertRaises(DecompError):
                site_enhanced.settings(bad)


class Reading(unittest.TestCase):
    def reply(self, doc):
        body = mock.MagicMock()
        body.__enter__.return_value = SimpleNamespace(read=lambda *_: json.dumps(doc).encode())
        return body

    def test_eta_list_posts_the_action_and_keys_rows_by_card(self):
        row = {"Identity": {"Card": "c1"}, "Description": {"Step": 1}}
        doc = {"Metadata": {"Result": "Success"}, "Response": [row]}
        with mock.patch("urllib.request.urlopen", return_value=self.reply(doc)) as urlopen:
            self.assertEqual(site_enhanced.eta_rows("http://butler.example.lan:1/", "b"), {"c1": {"Step": 1}})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://butler.example.lan:1/Eta/List")
        self.assertEqual(json.loads(request.data), {"Metadata": {}, "Request": {"Scope": "b"}})

    def test_a_fault_reply_is_refused_with_its_code(self):
        doc = {"Metadata": {"Result": "Fault"}, "Response": {"Error": {"Code": "ExecutionFailed", "Message": "no"}}}
        with (
            mock.patch("urllib.request.urlopen", return_value=self.reply(doc)),
            self.assertRaisesRegex(DecompError, "ExecutionFailed"),
        ):
            site_enhanced.eta_rows("http://butler.example.lan:1", "b")


@unittest.skipUnless(shutil.which("node"), "node runs the page module's pure logic")
class PageLogic(unittest.TestCase):
    def js(self, expr):
        script = f'import * as m from "{JS.as_uri()}"; process.stdout.write(JSON.stringify({expr}));'
        out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_shares_split_the_bar_in_state_order(self):
        got = self.js("m.shares({closed: 6, working: 3, open: 11})")
        self.assertEqual([(g["state"], g["count"]) for g in got], [("closed", 6), ("working", 3), ("open", 11)])
        self.assertEqual(sum(g["percent"] for g in got), 100)
        self.assertEqual(self.js("m.shares({closed: 0, working: 0, open: 0})")[0]["percent"], 0)


if __name__ == "__main__":
    unittest.main()
