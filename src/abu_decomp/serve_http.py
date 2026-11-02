"""The one reply path of `decomp tas serve`: every route module answers through `Replies`.
A text body carries an ETag (a matching If-None-Match gets a 304) and goes out gzipped when the client accepts it."""

from __future__ import annotations

import hashlib
import json
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any

from abu_decomp import DecompError

GZIP_MIN = 1024  # smaller bodies go out as they are
TEXT_KINDS = ("application/json", "text/html", "text/css", "text/javascript", "image/svg+xml")


def body_tag(body: bytes) -> str:
    return '"' + hashlib.blake2b(body, digest_size=10).hexdigest() + '"'


def tag_matches(header: str | None, tag: str) -> bool:
    """An If-None-Match header (a list, weak tags allowed) names `tag`."""
    return bool(header) and any(part.strip().removeprefix("W/") in (tag, "*") for part in str(header).split(","))


def query_args(query: str) -> dict[str, str]:
    """`a=1&b=2` as a dict (a bare key maps to "")."""
    return dict(part.partition("=")[::2] for part in query.split("&") if part)


def accepts_gzip(headers: Any) -> bool:
    return "gzip" in headers.get("Accept-Encoding", "")


class Replies(BaseHTTPRequestHandler):
    """The base of the serve handler: HTTP/1.1, no access log, and the reply helpers the route modules call."""

    protocol_version = "HTTP/1.1"
    site: Any  # the Server these requests are answered for (set by tas_serve.handler)

    def setup(self) -> None:
        super().setup()
        # A frame/PCM fragment must not wait for a delayed ACK on HTTP/1.1 phones.
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def log_message(self, *args: Any) -> None:
        pass

    def reply(
        self,
        body: bytes,
        kind: str,
        status: int = HTTPStatus.OK,
        tag: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> None:
        """Send a body. Text kinds carry an ETag (`tag`, else a hash of the body): a matching If-None-Match gets
        a 304 with no body, and a client that accepts gzip gets the compressed body. Others are sent as they are."""
        headers = dict(extra or {})
        text = kind.split(";")[0] in TEXT_KINDS and status == HTTPStatus.OK
        if text:
            tag = tag or body_tag(body)
            headers["ETag"] = tag
            headers["Vary"] = "Accept-Encoding"
            if tag_matches(self.headers.get("If-None-Match"), tag):
                self.send_response(HTTPStatus.NOT_MODIFIED)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            if len(body) >= GZIP_MIN and accepts_gzip(self.headers):
                body = self.site.gzipped(tag, body)
                headers["Content-Encoding"] = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if text else "no-store")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        self.reply(json.dumps(value).encode(), "application/json", status)

    def missing(self, what: str = "not found") -> None:
        self.json({"error": what}, HTTPStatus.NOT_FOUND)

    def stream_head(self, kind: str, extra: dict[str, str] | None = None) -> None:
        """The head of a stream that ends only when either side closes: no store, no proxy buffering, no keep-alive."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True


def need_id(body: dict[str, Any], noun: str = "run") -> str:
    """The `id` a POST names; a body without one is refused saying so (an empty id names nothing)."""
    found = str(body.get("id") or "")
    if not found:
        raise DecompError("tas.request", f'the body names no {noun}: send {{"id": ...}}')
    return found
