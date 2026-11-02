# SPDX-License-Identifier: GPL-3.0-or-later
"""lockstep.py check RUN on reach.bin and its run.json census (CONTRACTS.md Run record): a run whose reach cannot be
proved over the site's fnrows is refused, never defaulted."""

import json

from test_run_coverage import FNROWS, NAMES, lockstep, make_run


def census_problems(run):
    return [p for p in lockstep.check_run(run)["problems"] if p.startswith(("run.json", "census", "reach.bin"))]


def test_a_reach_bin_over_its_census_holds(tmp_path):
    assert census_problems(make_run(tmp_path)) == []


def test_a_run_without_reach_or_census_has_no_reach_to_check(tmp_path):
    run = make_run(tmp_path, census=False)
    (run / "reach.bin").unlink()
    assert census_problems(run) == []


def test_reach_bin_without_census_is_refused(tmp_path):
    assert census_problems(make_run(tmp_path, census=False)) == ["run.json census: missing beside reach.bin"]


def test_reach_bin_of_the_wrong_length_is_refused(tmp_path):
    run = make_run(tmp_path)
    (run / "reach.bin").write_bytes(b"\x01")
    assert census_problems(run) == ["reach.bin: 1 bytes, expected ceil(11/8) = 2"]


def test_set_pad_bits_are_refused(tmp_path):
    run = make_run(tmp_path, reached=(0, len(NAMES)))  # bit 11 lies past the last of the 11 rows
    assert census_problems(run) == ["reach.bin: pad bits set past row 11"]


def test_a_stale_census_sha256_is_refused(tmp_path):
    run = make_run(tmp_path)
    (tmp_path / "runs" / "site" / FNROWS).write_text("Other\n" * len(NAMES))  # the site was rebaked under the name
    sha = json.loads((run / "run.json").read_text())["census"]["sha256"]
    assert census_problems(run) == [f"census sha256 {sha}: the site's {FNROWS} no longer has it"]


def test_check_exits_1_on_a_bad_census(tmp_path):
    run = make_run(tmp_path, census=False)
    assert lockstep.main(["check", str(run)]) == 1
