"""The wire format of the `/video/RUN` and `/wall` streams (VIDEO_HEADER + body, CONTRACTS.md); those two handlers
themselves are in tas_serve. /video/RUN is the run's one picture path: the port's latest picture rides it, so there is
no still route of its own (the oracle's latest is /still/RUN?side=oracle)."""

from __future__ import annotations

import struct

VIDEO_FPS = 30  # most pictures a second: the latest one, no backlog or replay of missed pictures
VIDEO_HEADER = struct.Struct(">IQI")  # JPEG length, capture stamp (microseconds), input sequence
REACH_FLAG = 0x80000000  # set in a /video header's length field: the body is a reach record, not a JPEG
WALL_TABLE = REACH_FLAG  # set in a /wall header's length field: the body is JSON {"id": run id} naming run index `s
WALL_FPS = 4
