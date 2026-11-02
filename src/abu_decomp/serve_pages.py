"""The pages of `decomp tas serve` that are not static files: the Runner shell at `/` and `/watch`, a redirect to
the play view of the newest running run (`/#/play/RUN`, the run's data path)."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

PAGE = Path(__file__).with_name("runner.html").read_text  # the page file
SHA_MARK = "__DEPLOYED_SHA__"  # in the page file: replaced by the commit the toolkit was deployed at
HTML = "text/html; charset=utf-8"


def index(req: Any, server: Any, query: str = "") -> None:
    req.reply(PAGE().replace(SHA_MARK, server.deployed).encode(), HTML)


def watch(req: Any, server: Any, query: str = "") -> None:
    """302 to `/#/play/<newest running run>`; with no run going, a refusal naming that (404)."""
    running = server.live.running
    if not running:
        raise DecompError("watch.notfound", "no run is going: start a play, then open /watch")
    req.reply(b"", "text/plain", HTTPStatus.FOUND, extra={"Location": f"/#/play/{max(running).name}"})
