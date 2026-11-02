"""The live picture routes of `decomp tas serve`: `/frame/RUN`, `/oracle/RUN`, `/video/RUN` (one run's sampled JPEG
stream) and `/wall` (every running run's thumbs on one stream). Their wire format is VIDEO_HEADER + body
(CONTRACTS.md)."""

from __future__ import annotations

import json
import socket
import struct
from pathlib import Path
from typing import Any

from abu_decomp import tas, tas_oracle
from abu_decomp.serve_pace import Beat, clock

VIDEO_FPS = 30  # latest frame sampled at wall pace; no backlog or replay of missed pictures
VIDEO_HEADER = struct.Struct(">IQI")  # JPEG length, capture stamp (microseconds), input sequence
REACH_FLAG = 0x80000000  # set in a /video header's length field: the body is a reach record, not a JPEG
WALL_TABLE = REACH_FLAG  # set in a /wall header's length field: the body is JSON {"id": run id} naming run index `s
WALL_KEEPALIVE_INDEX = 0xFFFFFFFF  # a flagged, empty /wall record: nothing to show, but a closed client is noticed
WALL_FPS = 4
WALL_KEEPALIVE_SECONDS = 10.0


def frame(req: Any, server: Any, run: Path, query: str = "", name: str = tas.VIEW) -> None:
    """The run's latest picture as a PNG; `name` is the file in the run folder (the oracle's is tas_oracle.VIEW)."""
    try:
        stamp, body, seq = server.capture(run / name)
    except (OSError, ValueError):
        return req.missing()
    return req.reply(body, "image/png", extra={"X-Frame": str(stamp), "X-Input-Seq": str(seq)})


def oracle(req: Any, server: Any, run: Path, query: str = "") -> None:
    """The original game's latest picture of a run played with the oracle."""
    frame(req, server, run, query, tas_oracle.VIEW)


def video(req: Any, server: Any, run: Path, query: str = "") -> None:
    """One bounded latest-frame stream: 30 samples/s, independent of RTT and turbo's game clock."""
    req.stream_head("application/x-tas-frames", {"X-Video-Fps": str(VIDEO_FPS), "Connection": "close"})
    # A stalled phone cannot accumulate seconds of pictures in the server's socket buffer.
    req.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
    req.connection.settimeout(5)
    stamp, sent_seq, beat = 0, 0, Beat(1 / VIDEO_FPS)
    feed = server.reach.feed_for(run)  # the run's reach records (section 10a), None when it has none
    first = feed.first() if feed else None
    while True:
        try:
            current, body, seq = server.capture(run / tas.VIEW, "JPEG")
        except (OSError, ValueError):
            current, body, seq = stamp, b"", 0
        changed = current != stamp
        # Reach records go out ahead of the picture they belong to; a new viewer's first bytes are a full one.
        records = ([first] if first else []) + (feed.pending() if feed else [])
        first = None
        out = b"".join(VIDEO_HEADER.pack(len(r) | REACH_FLAG, current, sent_seq) + r for r in records)
        if changed:
            out += VIDEO_HEADER.pack(len(body), current, seq) + body
        if out:
            try:
                req.wfile.write(out)
                req.wfile.flush()
            except OSError:
                return  # disconnect/backpressure timeout: release this viewer immediately
        if changed:
            stamp, sent_seq = current, seq
        if not tas.running(run):
            return
        # When the producer is late, wait for its next capture without losing another whole period.
        beat.wait(advance=changed, floor=0.002)


def wall(req: Any, server: Any, query: str = "") -> None:
    """One multiplexed stream of WALL_WIDTH px JPEG thumbs of every running run, WALL_FPS a second. A frame is
    VIDEO_HEADER (length, stamp, run index in the place of the input sequence) + JPEG. The first time a run
    shows, a record with WALL_TABLE set in its length (body: JSON {"id": run id}) names its index; indices are
    per connection and never reused. Each run's reach records (section 10a) ride flagged under its index too, a
    full one first: the body of a flagged record is the table when it starts with "{", else a reach record.
    Browsers allow six connections per host, so twelve per-tile streams would stall."""
    req.stream_head("application/x-tas-wall", {"X-Wall-Fps": str(WALL_FPS), "Connection": "close"})
    req.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
    req.connection.settimeout(5)
    index: dict[str, int] = {}
    stamps: dict[str, int] = {}
    feeds: dict[str, Any] = {}
    quiet, beat = clock(), Beat(1 / WALL_FPS)
    while True:
        out = b""
        for run in server.running_runs():
            try:
                stamp, body, _ = server.capture(run / tas.VIEW, "THUMB")
            except (OSError, ValueError):
                continue
            if run.name not in index and len(index) < WALL_KEEPALIVE_INDEX:
                index[run.name] = len(index)
                name = json.dumps({"id": run.name}).encode()
                out += VIDEO_HEADER.pack(len(name) | WALL_TABLE, 0, index[run.name]) + name
                feeds[run.name] = feed = server.reach.feed_for(run)
                first = feed.first() if feed else None
                if first and any(first[5:]):  # a run that lit nothing yet costs the wall no record
                    out += VIDEO_HEADER.pack(len(first) | REACH_FLAG, stamp, index[run.name]) + first
            feed = feeds.get(run.name)
            for record in feed.pending() if feed else []:
                out += VIDEO_HEADER.pack(len(record) | REACH_FLAG, stamp, index[run.name]) + record
            if run.name in index and stamps.get(run.name) != stamp:
                stamps[run.name] = stamp
                out += VIDEO_HEADER.pack(len(body), stamp, index[run.name]) + body
        current = clock()
        if not out and current - quiet >= WALL_KEEPALIVE_SECONDS:
            out = VIDEO_HEADER.pack(WALL_TABLE, 0, WALL_KEEPALIVE_INDEX)
        if out:
            quiet = current
            try:
                req.wfile.write(out)
                req.wfile.flush()
            except OSError:
                return
        beat.wait()  # a round's own work does not slow the pace
