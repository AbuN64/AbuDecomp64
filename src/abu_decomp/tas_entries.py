# SPDX-License-Identifier: GPL-3.0-or-later
"""Oracle/port entry counts through the ordinary TAS replay boundary."""

import collections
import dataclasses
import itertools
import json
import subprocess
from pathlib import Path

from abu_decomp import DecompError, tas
from abu_decomp.config import Profile


def window(text: str) -> tuple[int, int]:
    try:
        lo, hi = map(int, text.split(":"))
        if 0 <= lo <= hi:
            return lo, hi
    except ValueError:
        pass
    raise DecompError("tas.entries", "--window must be inclusive nonnegative FROM:TO")


def compare(oracle: Path, port: Path, nm: str, manifest: Path, functions: list[str], lo: int, hi: int) -> dict:
    bindings = json.loads(manifest.read_text()).get("bindings", {})

    def norm(name):
        name = bindings.get(name, name) if isinstance(bindings.get(name, name), str) else name
        for suffix in ("_recomp", "_kept"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        if name.startswith("addrLow_") and name.endswith("_de"):
            name = name[8:-3]
        return name

    names = {}
    for line in nm.splitlines():
        words = line.split()
        if len(words) == 3 and words[1] in "tTwW":
            names.setdefault(int(words[0], 16), norm(words[2]))
    for side, path in (("oracle", oracle), ("port", port)):
        if not path.is_file() or path.stat().st_size == 0:
            raise DecompError("tas.entries", f"{side} entry trace is missing or empty: {path}")
    sequences = []
    coverage = []
    for path, is_port in ((oracle, False), (port, True)):
        seq = collections.defaultdict(list)
        covered = set()
        with path.open() as stream:
            for line in stream:
                words = line.split()
                if len(words) < (4 if is_port else 3):
                    raise DecompError("tas.entries", f"malformed entry in {path}: {line.rstrip()}")
                vi = int(words[0])
                if lo <= vi <= hi:
                    covered.add(vi)
                    raw = words[3] if is_port else words[2]
                    if is_port and raw.startswith("sc:"):
                        continue
                    name = names.get(int(raw, 16), raw) if is_port else raw
                    name = norm(name)
                    if name in functions:
                        seq[vi].append(name)
        sequences.append(seq)
        coverage.append(covered)
    missing = [sorted(set(range(lo, hi + 1)) - covered) for covered in coverage]
    if any(missing):
        raise DecompError("tas.entries", f"trace does not cover window; missing oracle/port retraces: {missing}")
    rows, totals, differing = [], {}, 0
    for vi in range(lo, hi + 1):
        o, p = (collections.Counter(seq[vi]) for seq in sequences)
        counts = {fn: {"oracle": o[fn], "port": p[fn]} for fn in functions}
        bad = any(o[fn] != p[fn] for fn in functions)
        differing += bad
        rows.append({"retrace": vi, "counts": counts, "oracle_order": sequences[0][vi], "differs": bad})
    for fn in functions:
        totals[fn] = {side: sum(row["counts"][fn][side] for row in rows) for side in ("oracle", "port")}
    return {"rows": rows, "totals": totals, "differing": differing, "exit": int(bool(differing))}


def write_tsv(path: Path, result: dict, functions: list[str]) -> None:
    with path.open("w") as stream:
        stream.write(
            "retrace\t"
            + "\t".join(f"{side}:{fn}" for fn in functions for side in ("oracle", "port"))
            + "\toracle_order\n"
        )
        for row in result["rows"]:
            counts = [str(row["counts"][fn][side]) for fn in functions for side in ("oracle", "port")]
            runs = " ".join(f"{fn}*{sum(1 for _ in group)}" for fn, group in itertools.groupby(row["oracle_order"]))
            stream.write("\t".join([str(row["retrace"]), *counts, runs]) + "\n")


def run(profile: Profile, movie: str, retraces: str, fn: str, port: Path, manifest: Path, out: Path) -> dict:
    lo, hi = window(retraces)
    functions = fn.split(",")
    if not all(functions) or len(set(functions)) != len(functions):
        raise DecompError("tas.entries", "--fn must contain distinct nonempty function names")
    port, manifest, out = port.resolve(), manifest.resolve(), out.resolve()
    if not port.is_file() or not manifest.is_file():
        raise DecompError("tas.entries", "--port and --manifest must be files")
    table = profile.section("tas")
    if not table.get("oracle"):
        raise DecompError("tas.entries", "tas.oracle is required")
    out.mkdir(parents=True, exist_ok=False)
    oracle_trace, port_trace = out / "oracle.entries", out / "port.entries"
    table.setdefault("env", {})["N64C_ENTRY_TRACE"] = f"{port_trace}:{lo}:{hi}"
    table["oracle"].setdefault("env", {})["N64R_TRACE"] = str(oracle_trace)
    # Private runs prevent concurrent proofs from pruning or reusing each other's replay evidence.
    table["runs"] = str(out / "runs")
    replay_profile = dataclasses.replace(profile, _sections={"tas": table})
    played = tas.play(replay_profile, movie, port=port, oracle=True, state_diff=False)
    if played.get("outcome") != "ok" or played.get("oracle", {}).get("outcome") not in (None, "ok"):
        raise DecompError("tas.entries", f"replay failed: {played}")
    nm = subprocess.run(["nm", str(port)], check=True, capture_output=True, text=True).stdout
    (out / "port.nm").write_text(nm)
    result = compare(oracle_trace, port_trace, nm, manifest, functions, lo, hi)
    write_tsv(out / "entries.tsv", result, functions)
    (out / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    return {
        "movie": movie,
        "window": [lo, hi],
        "totals": result["totals"],
        "differing": result["differing"],
        "exit": result["exit"],
        "tsv": str(out / "entries.tsv"),
        "replay": played,
    }
