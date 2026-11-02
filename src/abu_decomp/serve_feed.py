"""`GET /api/feed/stream`: the site feed as Server-Sent Events."""

from __future__ import annotations

import contextlib
from typing import Any

from abu_decomp import serve_wake, site_core


def stream(req: Any, server: Any, query: str = "") -> None:
    """Server-Sent Events: a `hello` with the feed's version and a `feed` event for every event it holds (a page
    that read /api/feed before Live's first publish misses nothing), then one `feed` event per new or changed
    event when the feed changes (the Live watch's signal). The handler blocks on the feed's wake pipe and the
    connection, so a closed client ends it at once and an idle stream sends nothing."""
    req.stream_head("text/event-stream")
    pipe = server.feed.listen()
    try:
        with serve_wake.Wake(req.connection) as wake:
            wake.signal(pipe)
            version, held = server.feed.snapshot()
            req.wfile.write(site_core.sse("hello", {"version": version}))
            req.wfile.write(b"".join(site_core.sse("feed", item) for item in held))
            req.wfile.flush()
            while wake.wait():
                version, items = server.feed.changes(version)
                if items:
                    req.wfile.write(b"".join(site_core.sse("feed", item) for item in items))
                    req.wfile.flush()
    finally:
        with contextlib.suppress(KeyError):
            server.feed.unlisten(pipe)
