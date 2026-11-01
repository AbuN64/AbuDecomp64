"""Site region `enhanced`: the Enhanced page's record (`GET /api/enhanced`; card RUNNER-ENHANCED, RUNNER-ENHANCED-PLAN).

The page shows the Enhanced MVP plan. The plan's own files are the one source of its shape: `Split.md` (the rows, their
minutes and the rows each waits on), `MVP.md` (each MVP item and the rows that make it real) and `enhanced_made.json`
(row -> card), read from the directory the profile names. Each row's state and last line come from ButlerMaster's
public HTTP action `Eta/List` (a card with no row is closed, as Eta List drops a done card). The record has the MVP
items with their rows, the plan's longest chain (the critical path) and the finish line: the platform proofs and the
gate row(s) nothing else waits on. Its address, board, Kan address and plan directory come from the host's profile
section `tas.serve.enhanced`, never from tracked code. The answer is a held snapshot (site_core.Snapshots), rebuilt when
the feed's run events arrive, so no request reads ButlerMaster; nothing here polls.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

KEYS = frozenset({"butlermaster", "board", "kan", "plan"})
SPLIT_FILE, MVP_FILE, MADE_FILE = "Split.md", "MVP.md", "enhanced_made.json"
READ_SECONDS = 20.0  # the longest one Eta List read may take: an explicit bound, not a wait
PLAY_ROUTE = "#/play"
ROW_KEY = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")
PROOF_WORD = "proof"  # an MVP item whose name says proof is a platform proof: part of the finish line


def settings(section: Any) -> dict[str, Any] | None:
    """The validated `tas.serve.enhanced` section; None when the host does not configure the page."""
    if section is None:
        return None
    if not isinstance(section, dict):
        raise DecompError("tas.serve.enhanced", "tas.serve.enhanced must be an object")
    extra = section.keys() - KEYS
    if extra:
        raise DecompError("tas.serve.enhanced", f"unknown tas.serve.enhanced keys: {', '.join(sorted(extra))}")
    missing = KEYS - section.keys()
    if missing:
        raise DecompError("tas.serve.enhanced", f"tas.serve.enhanced lacks: {', '.join(sorted(missing))}")
    if not all(isinstance(section[key], str) for key in KEYS):
        raise DecompError("tas.serve.enhanced", "tas.serve.enhanced values are strings (plan is a directory path)")
    return {key: section[key] for key in KEYS}


def table(text: str, first: str) -> list[dict[str, str]]:
    """The markdown table whose header's first cell is `first`, as one dict per row keyed by header cell."""
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if header is None:
            header = cells if cells[0] == first else None
        elif set("".join(cells)) - set("-: "):
            rows.append(dict(zip(header, cells, strict=False)))
    if not rows:
        raise DecompError("site.enhanced", f"no table headed {first!r} in the plan")
    return rows


def keys_of(cell: str) -> list[str]:
    """The row keys named in a cell (`none` names none)."""
    return ROW_KEY.findall(cell)


def read_plan(directory: str) -> dict[str, Any]:
    """The plan from its own files: rows (key -> minutes, waits, card) and items (name, acceptance, row keys)."""
    root = Path(directory)
    try:
        split = table((root / SPLIT_FILE).read_text(), "key")
        mvp = table((root / MVP_FILE).read_text(), "Item")
        made = json.loads((root / MADE_FILE).read_text())
    except (OSError, ValueError) as error:
        raise DecompError("site.enhanced", f"the plan is unreadable in {directory}: {error}") from error
    rows = {}
    for row in split:
        key = row["key"]
        if key not in made:
            raise DecompError("site.enhanced", f"plan row {key} has no card in {MADE_FILE}")
        rows[key] = {"minutes": int(row["est_min"]), "waits": keys_of(row["waits"]), "card": made[key]}
    items = [
        {"name": row["Item"], "acceptance": row["Player-visible acceptance"], "rows": keys_of(row["Split rows"])}
        for row in mvp
    ]
    for item in items:
        unknown = [key for key in item["rows"] if key not in rows]
        if unknown:
            raise DecompError("site.enhanced", f"item {item['name']} names unknown rows: {', '.join(unknown)}")
    return {"rows": rows, "items": items}


def critical_path(rows: dict[str, dict[str, Any]]) -> list[str]:
    """The longest chain by minutes through the waits graph, first row first (ties keep the earlier key)."""
    best: dict[str, tuple[int, list[str]]] = {}

    def chain(key: str, stack: tuple[str, ...]) -> tuple[int, list[str]]:
        if key in stack:
            raise DecompError("site.enhanced", f"the plan waits in a cycle at {key}")
        if key not in best:
            before = max(
                (chain(wait, (*stack, key)) for wait in rows[key]["waits"]), default=(0, []), key=lambda c: c[0]
            )
            best[key] = (before[0] + rows[key]["minutes"], [*before[1], key])
        return best[key]

    return max((chain(key, ()) for key in rows), key=lambda c: c[0])[1]


def eta_rows(address: str, board: str) -> dict[str, dict[str, Any]]:
    """ButlerMaster's Eta List for `board`, by card: the seam tests replace."""
    body = json.dumps({"Metadata": {}, "Request": {"Scope": board}}).encode()
    request = urllib.request.Request(
        f"{address.rstrip('/')}/Eta/List", body, {"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=READ_SECONDS) as reply:
            doc = json.load(reply)
    except urllib.error.HTTPError as error:
        doc = json.load(error)
    except (OSError, ValueError) as error:
        raise DecompError("site.enhanced", f"ButlerMaster is unreachable: {error}") from error
    if doc.get("Metadata", {}).get("Result") != "Success":
        fail = (doc.get("Response") or doc).get("Error", {})
        raise DecompError("site.enhanced", f"Eta List refused: {fail.get('Code', '?')}: {fail.get('Message', doc)}")
    return {row["Identity"]["Card"]: row["Description"] for row in doc["Response"]}


def row_state(row: dict[str, Any] | None) -> dict[str, str]:
    """One plan row: no Eta row is closed; a row with a finished step is working; any other row is open."""
    if row is None:
        return {"state": "closed", "line": ""}
    state = "working" if row.get("Step", 0) > 0 else "open"
    line = row.get("Status") or (f"waits on {row['WaitsOn']}" if row.get("WaitsOn") else row.get("Eta", ""))
    return {"state": state, "line": line}


def item_state(states: list[str]) -> str:
    """An item is closed when all its rows are, open when none has moved, else working."""
    if all(state == "closed" for state in states):
        return "closed"
    return "open" if all(state == "open" for state in states) else "working"


def split_of(states: list[str]) -> dict[str, int]:
    return {state: states.count(state) for state in ("closed", "working", "open")}


def build_row(server: Any) -> dict[str, Any]:
    """The latest port build as live.json states it, with the route the existing play page answers."""
    build = server.state()["build"]
    return {**build, "play": PLAY_ROUTE}


def record(server: Any, config: dict[str, Any]) -> dict[str, Any]:
    plan = read_plan(config["plan"])
    eta = eta_rows(config["butlermaster"], config["board"])
    path = critical_path(plan["rows"])
    rows = {
        key: {
            "key": key,
            "card": row["card"],
            "minutes": row["minutes"],
            "waits": row["waits"],
            "critical": key in path,
            **row_state(eta.get(row["card"])),
        }
        for key, row in plan["rows"].items()
    }
    items = [
        {
            "name": item["name"],
            "acceptance": item["acceptance"],
            "state": item_state([rows[key]["state"] for key in item["rows"]]),
            "rows": [rows[key] for key in item["rows"]],
        }
        for item in plan["items"]
    ]
    waited_on = {wait for row in rows.values() for wait in row["waits"]}
    return {
        "kan": config["kan"],
        "plan": split_of([row["state"] for row in rows.values()]),
        "items": [item for item in items if PROOF_WORD not in item["name"].lower()],
        "critical": {"minutes": sum(rows[key]["minutes"] for key in path), "rows": [rows[key] for key in path]},
        "finish": {
            "proofs": [item for item in items if PROOF_WORD in item["name"].lower()],
            "gate": [row for key, row in rows.items() if key not in waited_on],
        },
        "build": build_row(server),
    }


def answer(server: Any, method: str, rest: str, query: str, body: bytes) -> tuple[int, Any]:
    if method != "GET":
        return HTTPStatus.METHOD_NOT_ALLOWED, {"error": f"{method} is not served here"}
    if rest:
        return HTTPStatus.BAD_REQUEST, {"error": "the Enhanced record is /api/enhanced"}
    if server.enhanced is None:
        return HTTPStatus.NOT_IMPLEMENTED, {"error": "tas.serve.enhanced is not set in this host's profile"}
    try:
        return HTTPStatus.OK, record(server, server.enhanced)
    except DecompError as error:
        return HTTPStatus.BAD_GATEWAY, {"error": error.detail}
