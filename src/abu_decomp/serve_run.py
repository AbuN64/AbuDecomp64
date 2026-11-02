"""The run-folder routes of `decomp tas serve`: `/shot/RUN` and `/diff/RUN` (the oracle's compared polls) and `/log/RUN`
(the run's output text). A run that is not there is refused 404 before any of them is called."""

from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, site_stream, tas, tas_oracle
from abu_decomp.serve_http import query_args


def shot(req: Any, server: Any, run: Path, query: str = "") -> None:
    """One side's picture at one compared poll: ?side=port|oracle&poll=N."""
    args = query_args(query)
    side, poll = args.get("side", ""), args.get("poll", "")
    if side not in tas_oracle.SIDES or not poll.isdigit():
        return req.missing()
    meta = (tas.read_record(run) or {}).get("oracle")
    if side == "oracle" and (cause := tas_oracle.gate_cause(meta)):
        return req.reply(cause.encode(), "text/plain; charset=utf-8", HTTPStatus.CONFLICT)
    try:
        _, body, _ = server.capture(run / tas_oracle.SHOTS / side / f"poll-{int(poll):06d}.ppm")
    except (OSError, ValueError):
        return req.missing()
    return req.reply(body, "image/png")


def diff(req: Any, server: Any, run: Path, query: str = "") -> None:
    """Every compared poll's score: {"every", "rows": [{"poll", "diff", "drift", "vi_drift"}]}."""
    row = tas.read_record(run)
    meta = row.get("oracle") if row else None
    if not row or not isinstance(meta, dict):
        return req.missing()
    summary = tas_oracle.summary(run, row) or {}
    every = int(meta.get("every") or 1)
    try:
        rows = tas_oracle.compare(run, every, summary.get("status") == "done", meta)
    except DecompError as error:
        return req.json({"every": every, "rows": [], "refused": error.detail})
    return req.json({"every": every, "rows": rows})


def log(req: Any, server: Any, run: Path, query: str = "") -> None:
    from_ = query_args(query).get("from", "")
    start = int(from_) if from_.isdigit() else 0
    try:
        with (run / "output.txt").open("rb") as handle:
            handle.seek(start)
            body = handle.read(1 << 20)
    except OSError:
        body = b""
    req.reply(body, "text/plain; charset=utf-8")


def stream(req: Any, server: Any, rest: str, query: str = "") -> None:
    """`/stream/RUN/SIDE/POLL.jpg`: one recorded frame of the lockstep stream (module site_stream)."""
    status, payload, kind = site_stream.frame(server, rest)
    req.reply(payload if isinstance(payload, bytes) else json.dumps(payload).encode(), kind, status)
