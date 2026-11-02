"""The board's JSON routes of `decomp tas serve`: `/api/coverage`, `/api/pool` and `/api/runs` (a page of finished
runs)."""

from __future__ import annotations

from typing import Any

from abu_decomp.serve_http import query_args


def coverage(req: Any, server: Any, query: str = "") -> None:
    req.json(server.coverage())


def pool(req: Any, server: Any, query: str = "") -> None:
    req.json(server.slot_pool())


def runs(req: Any, server: Any, query: str = "") -> None:
    args = query_args(query)
    before = float(args["before"]) if args.get("before") else None
    req.json(server.history(before, int(args["limit"]) if args.get("limit") else None))
