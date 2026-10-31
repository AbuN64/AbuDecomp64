"""Site region `enhanced`: the Enhanced progress page's record (`GET /api/enhanced`; card RUNNER-ENHANCED).

Three parts: the Enhanced story's children as closed / working / open (the split ButlerMaster's Eta List gives the story
row), each MVP item with its state and last line (its card's Eta List row; a card with no row is closed, as Eta List
drops a done card), and the latest port build with the link to the existing play page. ButlerMaster is read through its
public HTTP action `Eta/List`; its address, the board, the story and the MVP items' cards come from the host's profile
section `tas.serve.enhanced` (`butlermaster`, `board`, `story`, `items`), never from tracked code. The page refetches
when the feed's run events arrive; nothing here polls.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from http import HTTPStatus
from typing import Any

from abu_decomp import DecompError

KEYS = frozenset({"butlermaster", "board", "story", "items"})
SPLIT = re.compile(r"(\d+) closed / (\d+) working / (\d+) open")
READ_SECONDS = 20.0  # the longest one Eta List read may take: an explicit bound, not a wait
PLAY_ROUTE = "#/play"


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
    items = section["items"]
    if not isinstance(items, dict) or not all(isinstance(card, str) for card in items.values()):
        raise DecompError("tas.serve.enhanced", "tas.serve.enhanced.items maps an item name to its card")
    return {key: section[key] for key in KEYS}


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


def story_split(row: dict[str, Any] | None) -> dict[str, int]:
    """closed / working / open from the story row's Progress."""
    got = SPLIT.search((row or {}).get("Progress", ""))
    if not got:
        raise DecompError("site.enhanced", "the story has no closed / working / open row in Eta List")
    return dict(zip(("closed", "working", "open"), map(int, got.groups())))


def item_row(name: str, card: str, row: dict[str, Any] | None) -> dict[str, Any]:
    """One MVP item: no row is closed; a row with a finished step is working; any other row is open."""
    if row is None:
        return {"name": name, "card": card, "state": "closed", "line": ""}
    state = "working" if row.get("Step", 0) > 0 else "open"
    line = row.get("Status") or (f"waits on {row['WaitsOn']}" if row.get("WaitsOn") else row.get("Eta", ""))
    return {"name": name, "card": card, "state": state, "line": line}


def build_row(server: Any) -> dict[str, Any]:
    """The latest port build as live.json states it, with the route the existing play page answers."""
    build = server.state()["build"]
    return {**build, "play": PLAY_ROUTE}


def record(server: Any, config: dict[str, Any]) -> dict[str, Any]:
    rows = eta_rows(config["butlermaster"], config["board"])
    return {
        "story": {"card": config["story"], **story_split(rows.get(config["story"]))},
        "items": [item_row(name, card, rows.get(card)) for name, card in config["items"].items()],
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
