"""The picture routes of `decomp tas serve`: `/frame/RUN` and `/oracle/RUN`, and the wire format of the `/video/RUN`
and `/wall` streams (VIDEO_HEADER + body, CONTRACTS.md); those two handlers themselves are in tas_serve."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from abu_decomp import tas, tas_oracle

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
