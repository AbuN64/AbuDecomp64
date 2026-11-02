"""The pages of `decomp tas serve` that are not static files: the Runner shell at `/`, the display list at `/live`,
the site's own live game at `/watch` and a display's viewer at `/viewer/ID` (its RFB socket at `/viewer/ID/ws`, its
sound at `/viewer/ID/audio`)."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, display, viewer
from abu_decomp.serve_audio import wav_header

PAGE = Path(__file__).with_name("runner.html").read_text  # the page file
SHA_MARK = "__DEPLOYED_SHA__"  # in the page file: replaced by the commit the toolkit was deployed at
HTML = "text/html; charset=utf-8"


def index(req: Any, server: Any, query: str = "") -> None:
    req.reply(PAGE().replace(SHA_MARK, server.deployed).encode(), HTML)


def live(req: Any, server: Any, query: str = "") -> None:
    req.reply(viewer.live_page(display.records(server.displays)).encode(), HTML)


def watch(req: Any, server: Any, query: str = "") -> None:
    req.reply(b"", "text/plain", HTTPStatus.FOUND, extra={"Location": server.keeper.watch()})


def viewer_audio(req: Any, server: Any, subject: str) -> None:
    """The display's sound as one growing WAV (s16le stereo), from this viewer's request until it leaves."""
    capture = display.backend(server.displays).audio(subject)
    try:
        req.send_response(HTTPStatus.OK)
        req.send_header("Content-Type", "audio/wav")
        req.send_header("Cache-Control", "no-store")
        req.send_header("X-Accel-Buffering", "no")
        req.send_header("Connection", "close")
        req.end_headers()
        req.close_connection = True
    except BaseException:
        capture.kill()
        capture.wait()
        raise
    viewer.stream_audio(req.wfile, capture, wav_header(display.AUDIO_RATE))


def viewer_page(req: Any, server: Any, rest: str, query: str = "") -> None:
    """A display's page, or with `/ws` its WebSocket upgrade that carries the display's RFB until either side closes,
    or with `/audio` its sound."""
    if rest.endswith("/audio"):
        return viewer_audio(req, server, rest[:-6])
    if not rest.endswith("/ws"):
        return req.reply(viewer.viewer_page(display.load(server.displays, rest)).encode(), HTML)
    record = display.load(server.displays, rest[:-3])
    key = req.headers.get("Sec-WebSocket-Key")
    if not key or req.headers.get("Upgrade", "").lower() != "websocket":
        raise DecompError("viewer.upgrade", "the viewer socket needs a WebSocket upgrade")
    req.close_connection = True
    viewer.serve_socket(req.connection, key, record, display.connect_address)
