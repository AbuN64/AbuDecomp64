"""`GET /api/feed/stream`: the site feed as Server-Sent Events."""

from __future__ import annotations

from typing import Any

from abu_decomp import site_core

FEED_KEEPALIVE_SECONDS = 15.0  # an idle /api/feed/stream sends a comment this often, so a closed client is noticed


def stream(req: Any, server: Any, query: str = "") -> None:
    """Server-Sent Events: a `hello` with the feed's version and a `feed` event for every event it holds (a page
    that read /api/feed before Live's first publish misses nothing), then one `feed` event per new or changed
    event when the feed changes (the Live watch's signal); a comment every FEED_KEEPALIVE_SECONDS when idle."""
    req.stream_head("text/event-stream")
    version, held = server.feed.snapshot()
    req.wfile.write(site_core.sse("hello", {"version": version}))
    req.wfile.write(b"".join(site_core.sse("feed", item) for item in held))
    req.wfile.flush()
    while True:
        version, items = server.feed.since(version, FEED_KEEPALIVE_SECONDS)
        req.wfile.write(b"".join(site_core.sse("feed", item) for item in items) or b": keepalive\n\n")
        req.wfile.flush()
