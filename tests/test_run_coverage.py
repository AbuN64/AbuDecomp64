# SPDX-License-Identifier: GPL-3.0-or-later
"""lockstep.py coverage --run RUN: the per-function parity join of reach.bin (over its run.json census), the port's
calls.jsonl and match.json (CONTRACTS.md Run record, coverage.tsv)."""

import contextlib
import hashlib
import importlib.util
import io
import json
import sys

import pytest

from abu_decomp import enhanced

LOCKSTEP = enhanced.shim_dir() / "boot" / "lockstep.py"  # RageWarsEnhanced owns the shim


def _load_lockstep():
    """RageWarsEnhanced's lockstep.py puts its own folder on sys.path when loaded; that shadowed tests/test_gates and
    tests/test_sweep with the shim's test_gates and test_sweep for the rest of discovery, so the path is put back."""
    before = list(sys.path)
    spec = importlib.util.spec_from_file_location("lockstep_under_test", LOCKSTEP)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = before
    return module


lockstep = _load_lockstep()

# A captured slice of the RWE-COVERAGE lane's coverage.tsv function column (fnrows order).
NAMES = [
    "AI_DynTurretCanSeePlayer",
    "AI_DynTurretGetTargetInfo",
    "AI_DynTurretOpposingPlayer",
    "Achieve_AcknowledgeResultsEvent",
    "Achieve_AdvanceResultModelPreview",
    "Achieve_ApplyLevelCompletionReward",
    "Achieve_ApplyPlayerOptionOverrides",
    "Achieve_ApplyProfileWeaponUnlocks",
    "Achieve_AwardAllMilestoneAchievement",
    "Achieve_AwardCharacterCompletionUnlock",
    "Achieve_AwardMilestoneCompletion",
]
FNROWS = "fnrows.0123abcd.txt"


def bitset(rows, size):
    bits = bytearray(size)
    for row in rows:
        bits[row >> 3] |= 1 << (row & 7)
    return bytes(bits)


def make_run(root, reached=(0, 1, 2, 4), calls=None, divergences=None, census=True, names=NAMES):
    """<root>/runs/{site/FNROWS, plays/r}: a finished lockstep run that holds field by field (CONTRACTS.md Run record):
    movie.raw, both sides' lockstep.jsonl, the port's calls, match.json, reach.bin and its census."""
    site, run = root / "runs" / "site", root / "runs" / "plays" / "r"
    site.mkdir(parents=True)
    for side, order in (("port", "native"), ("oracle", "canonical-be")):
        (run / "lockstep" / side).mkdir(parents=True)
        meta = {"k": "meta", "side": side, "ram": 16384, "page": 4096, "bytes": order, "hash": "fnv1a64-le64"}
        polls = [
            {"k": "poll", "poll": n, "vi": n, "ns": n * 16683333, "pad": ["80000000"], "pages": []} for n in range(9)
        ]
        (run / "lockstep" / side / "lockstep.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [meta, *polls]))
    data = "".join(n + "\n" for n in names).encode()
    (site / FNROWS).write_bytes(data)
    record = {"id": "r", "status": "done"}
    if census:
        record["census"] = {"fnrows": FNROWS, "sha256": hashlib.sha256(data).hexdigest(), "rows": len(names)}
    (run / "run.json").write_text(json.dumps(record))
    (run / "movie.raw").write_text("8000 0 0\n" * 9)
    (run / "reach.bin").write_bytes(bitset(reached, (len(names) + 7) // 8))
    if calls is None:  # fn -> call count: two calls of row 0, one of row 1, one of row 2, one of a row not reached
        calls = {NAMES[0]: 2, NAMES[1]: 1, NAMES[2]: 1, NAMES[3]: 1}
    # every kind trace.c writes (RWE docs/CONTRACTS.md, Port calls record), with its fields
    meta = {"k": "meta", "side": "port", "mode": "calls", "first": 16, "trace": len(names), "ring": "pow2"}
    meta.update(window=16, ordinal="thread", totals="poll")
    lines = [json.dumps(meta)]
    for pair, (fn, n) in enumerate(calls.items()):
        order = names.index(fn) + 1 if fn in names else 0
        lines.append(json.dumps({"k": "pair", "id": pair, "thr": 1, "caller": "c", "fn": fn}))
        lines.append(json.dumps({"k": "unframed", "order": order, "fn": fn, "pc": "0805a1c4"}))
        for o in range(n):
            call = {"k": "call", "order": order, "i": o, "thr": 1, "sp": "ffff4c78", "fn": fn, "frame": o}
            lines.append(json.dumps({**call, "caller": "c", "o": o, "a": ["00000000"] * 8}))
            ret = {"k": "ret", "order": order, "i": o, "thr": 1, "r": ["00000000", "00000001"], "st0": None}
            lines.append(json.dumps({**ret, "fn": fn, "caller": "c", "o": o}))
        lines.append(json.dumps({"k": "tot", "poll": 0, "d": [[pair, n]]}))
    lines.append('{"k": "call", "fn": "cut')  # a last line cut by a killed run
    (run / "lockstep" / "port" / "calls.jsonl").write_text("\n".join(lines))
    if divergences is None:
        word = {"poll": 3, "address": "80001000", "class": "value", "oracle": "00000001", "port": "00000002"}
        divergences = {"input": [], "vi": [], "clock": [], "memory": [word], "calls": []}
        divergences["calls"] = [{"fn": NAMES[1], "i": 0, "caller": "c", "poll": 0, "field": "args"}]
    rows = sorted((r for cls in divergences for r in divergences[cls]), key=lambda r: r["poll"])
    first = next((dict(r, **{"class": c}) for r in rows for c in divergences if r in divergences[c]), None)
    report = {"polls": {"oracle": 9, "port": 9, "common": 9}, "matched_through_poll": first["poll"] - 1 if first else 8}
    report.update(first=first, divergences=divergences, dump=None)
    (run / "lockstep" / "match.json").write_text(json.dumps(report))
    return run


def run_tool(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = lockstep.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def test_join_writes_one_row_per_census_row_in_fnrows_order(tmp_path):
    run = make_run(tmp_path)
    code, out, _ = run_tool("coverage", "--run", str(run))
    assert code == 0
    rows = (run / "coverage.tsv").read_text().split("\n")[:-1]
    assert rows[0] == "function\treached\tcalls\tdiverged\tequal"
    assert [r.split("\t")[0] for r in rows[1:]] == NAMES
    got = {r.split("\t")[0]: r.split("\t")[1:] for r in rows[1:]}
    assert got[NAMES[0]] == ["yes", "2", "0", "yes"]  # reached, called, never diverged
    assert got[NAMES[1]] == ["yes", "1", "1", "no"]  # a call divergence names it
    assert got[NAMES[2]] == ["yes", "1", "0", "yes"]
    assert got[NAMES[3]] == ["no", "1", "0", "no"]  # called but not reached: not equal
    assert got[NAMES[4]] == ["yes", "0", "0", "no"]  # reached but never called: no parity evidence
    sha = json.loads((run / "run.json").read_text())["census"]["sha256"]
    assert out.splitlines()[-1] == f"census {sha} rows={len(NAMES)} reached=4 equal=2"


def test_memory_divergences_are_movie_level_and_name_no_function(tmp_path):
    run = make_run(tmp_path)
    run_tool("coverage", "--run", str(run))
    assert sum(r.endswith("\tyes") for r in (run / "coverage.tsv").read_text().splitlines()) == 2


@pytest.mark.parametrize("missing", ["lockstep/match.json", "lockstep/port/calls.jsonl", "census"])
def test_a_missing_input_is_exit_2_and_no_join(tmp_path, missing):
    run = make_run(tmp_path, census=missing != "census")
    if missing != "census":
        (run / missing).unlink()
    code, _, err = run_tool("coverage", "--run", str(run))
    assert code == 2 and err
    assert not (run / "coverage.tsv").exists()


def test_the_plays_reach_bin_is_zero_filled_to_the_census_rows_never_cut(tmp_path):
    from abu_decomp import tas_reach

    tag = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    for rows, sent, want in ((70, bytes([0b1000]), bytes([0b1000]) + bytes(8)), (2, bytes([1, 2]), bytes([1, 2]))):
        run = tmp_path / str(rows)
        run.mkdir()
        collector = tas_reach.Collector(run, f"@abu-cov-{tag}-{rows}", f"@abu-cov-live-{tag}-{rows}", rows)
        collector.feed(tas_reach.full(1, sent))  # the port's bitset ends at the highest row it entered
        collector.finish()
        assert (run / "reach.bin").read_bytes() == want  # ceil(70/8) = 9 bytes; a longer one stays for check to refuse
