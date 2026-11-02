"""The Runner site's one stylesheet and page frame: every page links it (site_frame, static/site/site.css)."""

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

    def test_home_page_reaches_the_newest_run_in_one_click(self):
        self.assertIn('<a class="btn" href="/watch"', (SRC / "runner.html").read_text())


class Fake:
    deployed = "abc1234"

    def __init__(self, root: Path) -> None:
        self.feed = site_core.Feed()
        self.cfg = {"runs": root / "runs"}


class WalkTest(unittest.TestCase):
    """A private serve: every page the site answers as HTML links the shared stylesheet, and the stylesheet answers."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "runs" / "coverage").mkdir(parents=True)
        (root / "runs" / "coverage" / "index.html").write_bytes(REPORT)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), tas_serve.handler(Fake(root)))  # type: ignore[arg-type]
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    def tearDown(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.tmp.cleanup()

    def fetch(self, path: str) -> tuple[str, str]:
        with urllib.request.urlopen(self.base + path) as reply:
            return reply.headers["Content-Type"], reply.read().decode()

    def test_every_html_page_links_the_stylesheet(self):
        for path in ("/", "/coverage", "/coverage/"):
            kind, page = self.fetch(path)
            self.assertTrue(kind.startswith("text/html"), path)
            self.assertIn("/static/site/site.css", page, path)
            self.assertNotIn("<style", page, path)

    def test_the_stylesheet_is_served_as_css(self):
        kind, css = self.fetch("/static/site/site.css")
        self.assertEqual(kind, "text/css")
        self.assertIn("--accent", css)


if __name__ == "__main__":
    unittest.main()
