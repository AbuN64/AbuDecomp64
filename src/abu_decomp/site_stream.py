"""Site region `stream`: the lockstep stream of one recorded run (module tas_stream packs it).

GET /api/stream/RUN answers {run, polls, inputs, every}; GET /still/RUN?side=SIDE&poll=N (serve_run.still) answers one
frame. A request
packs the shots the run has dumped since the last pack first (the rig prunes them), so a running run's index grows as
its polls land. A refusal is a typed record {kind: "error", code, run, side, poll} with a 404 status, never prose.
"""

from __future__ import annotations

import contextlib
import json
from http import HTTPStatus
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, tas, tas_stream



def refusal(code: str, run: str, side: str | None = None, poll: str | None = None) -> tuple[int, dict[str, Any]]:
    return HTTPStatus.NOT_FOUND, {"kind": "error", "code": code, "run": run, "side": side, "poll": poll}


def folder(server: Any, run_id: str) -> Path | None:
    return tas.run_folder(server.cfg, run_id)


def refresh(run: Path) -> None:
    """Pack what the run has pictured since the last pack; a run with no pictures yet keeps what it has."""
    with contextlib.suppress(DecompError):
        tas_stream.pack(run)


def index(server: Any, run_id: str) -> tuple[int, dict[str, Any]]:
    run = folder(server, run_id)
    if run is None:
        return refusal("stream.run", run_id)
    refresh(run)
    try:
        return HTTPStatus.OK, json.loads((tas_stream.folder(run) / tas_stream.INDEX).read_text())
    except (OSError, ValueError):
        return refusal("stream.empty", run_id)


def answer(server: Any, method: str, rest: str, query: str, body: bytes) -> tuple[int, Any]:
    if method != "GET":
        return HTTPStatus.METHOD_NOT_ALLOWED, {"kind": "error", "code": "stream.method", "run": rest}
    return index(server, rest)
