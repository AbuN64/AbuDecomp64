"""Site region `map`: the sidebar grid. `/api/map` answers the ROM's functions as about a dozen region groups of squares
in two views, each a three-step scale.

A region is the source folder of a function's file (`src/game/LEVEL` is "Level"), ordered by first ROM address; a label
repeated by two folders is spelled with its parent folder. `groups_of` merges ROM-adjacent regions (the adjacent pair
with the fewest functions first, ties to the earlier pair) until at most GROUPS remain; a group is
`{label, members, count, bytes, rom}`, labelled by its largest member. A row is `[name, size, decomp, behaviour, group]`
in ROM order, where `decomp` and `behaviour` are the row's step (0, 1, 2) on each view's scale (`decomp_step`,
`behaviour_step`) and `group` its group's index. `VIEWS` names each view's steps for the always-visible legend.
The version is a hash of the groups and rows: the same data gives the same answer; the page redraws when it moves.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from http import HTTPStatus
from pathlib import Path
from typing import Any

GROUPS = 12  # region groups the sidebar draws: the owner's "about a dozen"
# Each view's three steps, lowest first. Decomp: assembly -> C that does not match yet (fuzzy) -> matching C.
# Behaviour: the port never ran it -> the port ran it and it differs or is not yet proven equal -> it behaves exactly.
VIEWS = (
    {"key": "decomp", "label": "Decomp", "steps": ["Assembly", "C, not matching", "Matching C"]},
    {"key": "behaviour", "label": "Behaviour", "steps": ["Not exercised", "Exercised, differs", "Exact behaviour"]},
)
EXERCISED = frozenset({"port", "both"})  # reach values where the port ran the function


def decomp_step(state: str) -> int:
    """0 assembly (or an unknown state), 1 fuzzy C, 2 matching C (exact or named)."""
    return 2 if state in ("exact", "named") else 1 if state == "fuzzy" else 0


def behaviour_step(reach: str, behaviour: str) -> int:
    """2 when a judged call behaves, 1 when one differs or the port ran it unjudged, else 0 (original-only included)."""
    if behaviour.startswith("behaves"):
        return 2
    if behaviour.startswith("differs") or reach in EXERCISED:
        return 1
    return 0


def region_label(key: str) -> str:
    """The proper-case name of a source folder: its last part, underscores as spaces ("src/game/LEVEL" -> "Level")."""
    return key.rsplit("/", 1)[-1].replace("_", " ").title() if key else "Unsorted"


def regions_of(files: list[str], rows: list[list[Any]], at: dict[str, int]) -> tuple[list[dict[str, Any]], list[int]]:
    """(regions, region index per row): functions grouped by the folder of their file, regions by first ROM address."""
    keys = [file.rsplit("/", 1)[0] if "/" in file else "" for file in files]
    order: dict[str, int] = {}
    for row in rows:
        order.setdefault(keys[row[at["file"]]], len(order))
    regions = [{"key": key, "label": region_label(key), "count": 0, "bytes": 0, "rom": -1} for key in order]
    labels = [region["label"] for region in regions]
    for region in regions:
        if labels.count(region["label"]) > 1:
            parts = region["key"].split("/")
            region["label"] = f"{region_label(parts[-2])} {region['label']}" if len(parts) > 1 else region["key"]
    index = []
    for row in rows:
        region = regions[order[keys[row[at["file"]]]]]
        region["count"] += 1
        region["bytes"] += row[at["size"]]
        region["rom"] = row[at["rom"]] if region["rom"] < 0 else min(region["rom"], row[at["rom"]])
        index.append(order[keys[row[at["file"]]]])
    return regions, index


def groups_of(regions: list[dict[str, Any]], target: int = GROUPS) -> tuple[list[dict[str, Any]], list[int]]:
    """(groups, group index per region): ROM-adjacent regions merged, the smallest adjacent pair first, to `target`."""
    spans = [[i] for i in range(len(regions))]
    while len(spans) > max(1, target):
        sizes = [sum(regions[i]["count"] for i in a + b) for a, b in itertools.pairwise(spans)]
        at = sizes.index(min(sizes))
        spans[at : at + 2] = [spans[at] + spans[at + 1]]
    groups, group_of = [], [0] * len(regions)
    for index, span in enumerate(spans):
        members = [regions[i] for i in span]
        largest = max(members, key=lambda region: (region["count"], -region["rom"]))
        roms = [region["rom"] for region in members if region["rom"] >= 0]
        groups.append(
            {
                "label": largest["label"],
                "members": [region["label"] for region in members],
                "count": sum(region["count"] for region in members),
                "bytes": sum(region["bytes"] for region in members),
                "rom": min(roms) if roms else -1,
            }
        )
        for i in span:
            group_of[i] = index
    return groups, group_of


def build(site: Path) -> dict[str, Any]:
    """The /api/map answer from the baked site."""
    manifest = json.loads((site / "manifest.json").read_text())
    baked = json.loads((site / manifest["fns"]).read_text())
    cols = baked["cols"]
    at = {name: cols.index(name) for name in ("name", "rom", "size", "state", "file", "reach", "behaviour")}
    rows = baked["rows"]
    regions, region_of = regions_of(baked["files"], rows, at)
    groups, group_of = groups_of(regions)
    out = [
        [
            row[at["name"]],
            row[at["size"]],
            decomp_step(row[at["state"]]),
            behaviour_step(row[at["reach"]] or "", row[at["behaviour"]] or ""),
            group_of[region_of[i]],
        ]
        for i, row in enumerate(rows)
    ]
    version = hashlib.sha1(json.dumps([out, groups], separators=(",", ":")).encode()).hexdigest()[:16]
    return {"version": version, "ref": baked.get("ref"), "views": list(VIEWS), "groups": groups, "rows": out}


def answer(server: Any, method: str, rest: str, query: str, body: bytes) -> tuple[int, Any]:
    if rest or method != "GET":
        return HTTPStatus.NOT_FOUND, {"error": "not an endpoint", "path": f"/api/map/{rest}", "method": method}
    try:
        return HTTPStatus.OK, build(server.site)
    except (OSError, ValueError, KeyError) as error:
        cause = f"{type(error).__name__}: {error}"
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no baked site to map", "cause": cause}
