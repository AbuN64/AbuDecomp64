"""The Runner site's one stylesheet and page frame: every page links it (site_frame, static/site/site.css)."""

import dataclasses
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from abu_decomp import site_core, site_frame, tas_serve

SRC = Path(__file__).resolve().parents[1] / "src" / "abu_decomp"
LINK = '<link rel="stylesheet" href="/static/site/site.css">'
REPORT = b"<html><head><title>gcovr</title></head><body><h1>Coverage</h1></body></html>"


class FrameTest(unittest.TestCase):
    def test_a_report_gets_the_stylesheet_in_head_and_the_header_in_body(self):
        page = site_frame.wrap_report(REPORT).decode()
        self.assertLess(page.index(LINK), page.index("</head>"))
        self.assertLess(page.index("<header"), page.index("<h1>"))
        self.assertEqual(site_frame.wrap_report(page.encode()).decode(), page)  # once

    def test_a_report_without_head_or_body_still_links_it(self):
        page = site_frame.wrap_report(b"<p>x").decode()
        self.assertTrue(page.startswith(LINK) and "<header" in page)

    def test_the_runner_shell_has_no_inline_style_and_links_the_stylesheet(self):
        shell = (SRC / "runner.html").read_text()
        self.assertIn(LINK, shell)
        self.assertNotIn("<style", shell)
        self.assertIn("--accent", (SRC / "static" / "site" / "site.css").read_text())

    def test_the_stylesheet_is_served_as_css(self):
        kind, css = self.fetch("/static/site/site.css")
        self.assertEqual(kind, "text/css")
        self.assertIn("--accent", css)


if __name__ == "__main__":
    unittest.main()
