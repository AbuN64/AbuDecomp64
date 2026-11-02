"""`/oracle/RUN` (the original game's latest picture) and the wire format of the `/video/RUN` and `/wall` streams
(VIDEO_HEADER + body, CONTRACTS.md); those two handlers themselves are in tas_serve. /video/RUN is the run's one
picture path: the port's latest picture rides it, so there is no still route of its own."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from abu_decomp import tas_oracle

VIDEO_FPS = 30  # latest frame sampled at wall pace; no backlog or replay of missed pictures
VIDEO_HEADER = struct.Struct(">IQI")  # JPEG length, capture stamp (microseconds), input sequence
REACH_FLAG = 0x80000000  # set in a /video header's length field: the body is a reach record, not a JPEG
WALL_TABLE = REACH_FLAG  # set in a /wall header's length field: the body is JSON {"id": run id} naming run index `s
WALL_KEEPALIVE_INDEX = 0xFFFFFFFF  # a flagged, empty /wall record: nothing to show, but a closed client is noticed
WALL_FPS = 4
WALL_KEEPALIVE_SECONDS = 10.0


def oracle(req: Any, server: Any, run: Path, query: str = "") -> None:
    """The original game's latest picture of a run played with the oracle, as a PNG."""
    try:
        stamp, body, seq = server.capture(run / tas_oracle.VIEW)
    except (OSError, ValueError):
        return req.missing()
    return req.reply(body, "image/png", extra={"X-Frame": str(stamp), "X-Input-Seq": str(seq)})
