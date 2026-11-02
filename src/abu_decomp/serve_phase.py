"""The phase-map routes of `decomp tas serve`: `/api/phase`, `/phase/shot` and `/phase/frame.png` (module
tas_phasemap)."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from abu_decomp.serve_http import query_args


def phase(req: Any, server: Any, query: str = "") -> None:
    req.json(*server.phase.phase())


def shot(req: Any, server: Any, query: str = "") -> None:
    found = server.phase.shot(query_args(query).get("platform") or "")
    if found is None:
        req.json({"error": "no screenshot for that platform"}, HTTPStatus.NOT_FOUND)
    else:
        req.reply(found[0], found[1])


def frame(req: Any, server: Any, query: str = "") -> None:
    version = query_args(query).get("version") or server.phase.focus()
    image, why = server.phase.frame(version) if version else (None, "no version")
    if image is None:
        req.json({"error": why}, HTTPStatus.NOT_FOUND)
    else:
        req.reply(image, "image/png")
