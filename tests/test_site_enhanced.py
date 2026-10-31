"""The Enhanced progress page's record (module site_enhanced) and the pure logic of static/site/enhanced.js. The
ButlerMaster seam (`eta_rows`) is mocked: no board is read."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import DecompError, site_core, site_enhanced

JS = Path(site_enhanced.__file__).parent / "static" / "site" / "enhanced.js"
CONFIG = {
    "butlermaster": "http://butler.example.lan:1",
    "board": "board-1",
    "story": "story-1",
    "items": {"exit": "card-exit", "bindings": "card-bind", "packaging": "card-pack"},
}
ROWS = {
    "story-1": {"Progress": "6 closed / 3 working / 14 open", "Step": 0},
    "card-bind": {"Step": 2, "Status": "Step 2 of 4: the bind table is read", "Eta": "~5 min"},
    "card-pack": {"Step": 0, "Eta": "no step finished", "WaitsOn": "the port release preset"},
}
BUILD = {"port": "/p/port", "pin": "a" * 40, "built": 1.5, "format": 3}


def server(config=CONFIG):
    return SimpleNamespace(enhanced=config, state=lambda: {"build": BUILD, "runs": []})


class Record(unittest.TestCase):
    def test_the_record_joins_the_story_split_the_items_and_the_build(self):
        with mock.patch.object(site_enhanced, "eta_rows", return_value=ROWS) as eta:
            status, got = site_enhanced.answer(server(), "GET", "", "", b"")
        eta.assert_called_once_with("http://butler.example.lan:1", "board-1")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(got["story"], {"card": "story-1", "closed": 6, "working": 3, "open": 14})
        self.assertEqual(
            got["items"],
            [
                {"name": "exit", "card": "card-exit", "state": "closed", "line": ""},
                {"name": "bindings", "card": "card-bind", "state": "working", "line": "Step 2 of 4: the bind table is read"},
                {"name": "packaging", "card": "card-pack", "state": "open", "line": "waits on the port release preset"},
            ],
        )
        self.assertEqual(got["build"], {**BUILD, "play": "#/play"})

    def test_a_story_without_a_split_is_refused_not_guessed(self):
        with mock.patch.object(site_enhanced, "eta_rows", return_value={"story-1": {"Progress": "50%"}}):
            status, got = site_enhanced.answer(server(), "GET", "", "", b"")
        self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
        self.assertIn("closed / working / open", got["error"])

    def test_butlermaster_refusing_shows_its_cause(self):
        with mock.patch.object(site_enhanced, "eta_rows", side_effect=DecompError("site.enhanced", "unreachable")):
            status, got = site_enhanced.answer(server(), "GET", "", "", b"")
        self.assertEqual((status, got), (HTTPStatus.BAD_GATEWAY, {"error": "unreachable"}))

    def test_an_unset_profile_section_answers_501_and_other_paths_refuse(self):
        self.assertEqual(site_enhanced.answer(server(None), "GET", "", "", b"")[0], HTTPStatus.NOT_IMPLEMENTED)
        self.assertEqual(site_enhanced.answer(server(), "POST", "", "", b"")[0], HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(site_enhanced.answer(server(), "GET", "x", "", b"")[0], HTTPStatus.BAD_REQUEST)

    def test_the_region_is_routed_through_the_site(self):
        with mock.patch.object(site_enhanced, "eta_rows", return_value=ROWS):
            status, got = site_core.answer(server(), "GET", "/api/enhanced", "", b"")
        self.assertEqual((status, got["story"]["working"]), (HTTPStatus.OK, 3))


class Settings(unittest.TestCase):
    def test_settings_are_validated(self):
        self.assertIsNone(site_enhanced.settings(None))
        self.assertEqual(site_enhanced.settings(dict(CONFIG)), CONFIG)
        for bad in ({**CONFIG, "url": "x"}, {k: v for k, v in CONFIG.items() if k != "board"}, {**CONFIG, "items": {"a": 1}}):
            with self.assertRaises(DecompError):
                site_enhanced.settings(bad)


class Reading(unittest.TestCase):
    def reply(self, doc):
        body = mock.MagicMock()
        body.__enter__.return_value = SimpleNamespace(read=lambda *_: json.dumps(doc).encode())
        return body

    def test_eta_list_posts_the_action_and_keys_rows_by_card(self):
        doc = {"Metadata": {"Result": "Success"}, "Response": [{"Identity": {"Card": "c1"}, "Description": {"Step": 1}}]}
        with mock.patch("urllib.request.urlopen", return_value=self.reply(doc)) as urlopen:
            self.assertEqual(site_enhanced.eta_rows("http://butler.example.lan:1/", "b"), {"c1": {"Step": 1}})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://butler.example.lan:1/Eta/List")
        self.assertEqual(json.loads(request.data), {"Metadata": {}, "Request": {"Scope": "b"}})

    def test_a_fault_reply_is_refused_with_its_code(self):
        doc = {"Metadata": {"Result": "Fault"}, "Response": {"Error": {"Code": "ExecutionFailed", "Message": "no"}}}
        with mock.patch("urllib.request.urlopen", return_value=self.reply(doc)):
            with self.assertRaisesRegex(DecompError, "ExecutionFailed"):
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
