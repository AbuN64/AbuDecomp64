# SPDX-License-Identifier: GPL-3.0-or-later
"""lockstep.py check RUN validates every Run record field (CONTRACTS.md Run record): a run that holds has no problem,
and each field-level corruption below is refused by name, alone."""

import hashlib
import json

import pytest
from test_run_coverage import NAMES, lockstep, make_run


def problems(run):
    return lockstep.check_run(run)["problems"]


def edit_json(path, change):
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def edit_lines(path, change):
    lines = path.read_text().split("\n")
    change(lines)
    path.write_text("\n".join(lines))


def record(change):
    return lambda run: edit_json(run / "run.json", change)


def report(change):
    return lambda run: edit_json(run / "lockstep" / "match.json", change)


def calls(change):
    return lambda run: edit_lines(run / "lockstep" / "port" / "calls.jsonl", change)


def census(key, value):
    return record(lambda r: r["census"].__setitem__(key, value))


def joined(change):
    """The run with its coverage.tsv written, then changed."""

    def apply(run):
        lockstep.write_coverage(run)
        edit_lines(run / "coverage.tsv", change)

    return apply


def lockstep_result(**fields):
    return record(lambda r: r.__setitem__("oracle", {"lockstep": {"exit": 1, **fields}}))


def write(path, data):
    def apply(run):
        (run / path).parent.mkdir(parents=True, exist_ok=True)
        (run / path).write_bytes(data)

    return apply


def stream(index=True, jpgs=True):
    def apply(run):
        for side in ("port", "oracle"):
            (run / "stream" / side).mkdir(parents=True)
            if jpgs:
                (run / "stream" / side / "4.jpg").write_bytes(b"\xff\xd8")
        if index:
            body = {"run": "r", "sides": ["port", "oracle"], "polls": [4], "inputs": [[0x8000, 0, 0]], "every": 4}
            (run / "stream" / "index.json").write_text(json.dumps(body))

    return apply


FIRST = {"fn": NAMES[1], "caller": "c", "o": 0, "poll": 0, "field": "args", "class": "calls"}  # make_run's first


def first_call(divergences):
    divergences["calls"][0].pop("fn")


CORRUPTIONS = {
    "movie-missing": (lambda run: (run / "movie.raw").unlink(), "movie.raw: missing"),
    "movie-empty": (write("movie.raw", b""), "movie.raw: no polls"),
    "movie-bad-pad": (write("movie.raw", b"8000 0 0\nzz 0\n"), "movie.raw line 2: 'zz 0'"),
    "movie-stick-range": (write("movie.raw", b"8000 200 0\n"), "movie.raw line 1: '8000 200 0'"),
    "movie-cut": (write("movie.raw", b"8000 0 0\n8000 0"), "movie.raw: its last poll is cut"),
    "retired-polls-raw": (write("polls.raw", b"8000 0 0\n"), "polls.raw: retired"),
    "retired-top-calls": (write("calls.jsonl", b""), "calls.jsonl: retired"),
    "retired-pages-bin": (write("lockstep/oracle/pages.bin", b""), "lockstep/oracle/pages.bin: retired"),
    "record-missing": (lambda run: (run / "run.json").unlink(), "run.json: missing"),
    "record-not-object": (write("run.json", b"[]"), "run.json: list, expected an object"),
    "oracle-side-missing": (lambda run: (run / "lockstep/oracle/lockstep.jsonl").unlink(), "lockstep/oracle: "),
    "calls-missing": (lambda run: (run / "lockstep/port/calls.jsonl").unlink(), "lockstep/port/calls.jsonl: missing"),
    "calls-meta": (
        calls(lambda ls: ls.__setitem__(0, '{"k": "meta", "side": "oracle"}')),
        "lockstep/port/calls.jsonl line 1",
    ),
    "calls-unreadable": (calls(lambda ls: ls.insert(2, "{not json")), "lockstep/port/calls.jsonl line 3: unreadable"),
    "calls-kind": (calls(lambda ls: ls.insert(1, '{"k": "hit"}')), "lockstep/port/calls.jsonl line 2: k 'hit'"),
    "calls-unframed-pc": (
        calls(lambda ls: ls.insert(1, '{"k": "unframed", "order": 3, "pc": "zz"}')),
        "lockstep/port/calls.jsonl line 2: an unframed line without its integer order and 8-hex pc",
    ),
    "calls-no-ordinal": (
        calls(lambda ls: ls.insert(1, '{"k": "ret", "fn": "f", "caller": "c", "r": []}')),
        "lockstep/port/calls.jsonl line 2: expected fn, caller and ordinal o",
    ),
    "calls-call-no-frame": (
        calls(lambda ls: ls.insert(1, '{"k": "call", "fn": "f", "caller": "c", "o": 9, "a": []}')),
        "lockstep/port/calls.jsonl line 2: a call without its poll frame",
    ),
    "calls-ret-no-words": (
        calls(lambda ls: ls.insert(1, '{"k": "ret", "fn": "f", "caller": "c", "o": 9}')),
        "lockstep/port/calls.jsonl line 2: a ret without its return words",
    ),
    "match-missing": (lambda run: (run / "lockstep/match.json").unlink(), "lockstep/match.json: "),
    "match-not-object": (write("lockstep/match.json", b"7"), "lockstep/match.json: int, expected an object"),
    "match-polls": (report(lambda r: r["polls"].__setitem__("port", -1)), "lockstep/match.json polls: "),
    "match-through": (
        report(lambda r: r.__setitem__("matched_through_poll", "1")),
        "lockstep/match.json matched_through_poll",
    ),
    "match-first": (report(lambda r: r.__setitem__("first", {"class": "sound"})), "lockstep/match.json first: "),
    "match-classes": (report(lambda r: r["divergences"].pop("vi")), "lockstep/match.json divergences: "),
    "match-row-poll": (
        report(lambda r: r["divergences"]["input"].append({"poll": "4"})),
        "lockstep/match.json divergences.input[0]: {'poll': '4'}, expected a row with its integer poll",
    ),
    "match-calls-fn": (report(lambda r: first_call(r["divergences"])), "lockstep/match.json divergences.calls[0]: "),
    "match-calls-field": (
        report(lambda r: r["divergences"]["calls"][0].__setitem__("field", "sound")),
        "lockstep/match.json divergences.calls[0]: ",
    ),
    "match-memory-class": (
        report(lambda r: r["divergences"]["memory"][0].__setitem__("class", "word")),
        "lockstep/match.json divergences.memory[0]: ",
    ),
    "match-memory-digits": (
        report(lambda r: r["divergences"]["memory"][0].__setitem__("port", "2")),
        "lockstep/match.json divergences.memory[0]: ",
    ),
    "match-first-without-rows": (
        report(lambda r: r["divergences"].update(calls=[], memory=[])),
        "lockstep/match.json first: ",
    ),
    "match-dump": (report(lambda r: r.__setitem__("dump", {"poll": 2, "pages": []})), "lockstep/match.json dump: "),
    "lockstep-exit": (lockstep_result(exit=3), "run.json oracle.lockstep: "),
    "lockstep-through": (
        lockstep_result(matched_through_poll=7, first=FIRST),
        "run.json oracle.lockstep matched_through_poll: 7, match.json holds -1",
    ),
    "census-fnrows-path": (census("fnrows", "../fnrows.txt"), "run.json census fnrows: '../fnrows.txt'"),
    "census-fnrows-type": (census("fnrows", 3), "run.json census fnrows: 3"),
    "census-sha256-form": (census("sha256", "ABC"), "run.json census sha256: 'ABC'"),
    "census-rows-zero": (census("rows", 0), "run.json census rows: 0"),
    "census-rows-bool": (census("rows", True), "run.json census rows: True"),
    "census-rows-stale": (census("rows", len(NAMES) + 1), f"census rows {len(NAMES) + 1}: the site's"),
    "census-extra-key": (census("version", 1), "run.json census: "),
    "census-without-reach": (lambda run: (run / "reach.bin").unlink(), "reach.bin: "),
    "coverage-without-census": (
        lambda run: (
            lockstep.write_coverage(run),
            (run / "reach.bin").unlink(),  # neither reach nor census: only the join is left without its census
            record(lambda r: r.pop("census"))(run),
        ),
        "coverage.tsv: no census to prove it over",
    ),
    "coverage-header": (joined(lambda ls: ls.__setitem__(0, "function\treached")), "coverage.tsv header: "),
    "coverage-row-count": (joined(lambda ls: ls.pop(1)), f"coverage.tsv: {len(NAMES) - 1} rows, the census has"),
    "coverage-order": (
        joined(lambda ls: ls.__setitem__(slice(1, 3), [ls[2], ls[1]])),
        "coverage.tsv function column: not the census rows in fnrows order",
    ),
    "coverage-equal-edited": (
        joined(lambda ls: ls.__setitem__(2, ls[2].replace("\tno", "\tyes"))),
        f"coverage.tsv line 3: '{NAMES[1]}\\tyes\\t1\\t1\\tyes', the join gives '{NAMES[1]}\\tyes\\t1\\t1\\tno'",
    ),
    "coverage-cut": (joined(lambda ls: ls.pop()), "coverage.tsv: its last row is cut"),
    "replay-name": (
        write("lockstep/replay/latest", b""),
        "lockstep/replay/latest: expected a folder named by the poll",
    ),
    "stream-no-index": (stream(index=False), "stream/index.json: "),
    "stream-missing-picture": (stream(jpgs=False), "stream/port/4.jpg: missing"),
    "stream-index-run": (
        lambda run: (stream()(run), edit_json(run / "stream/index.json", lambda i: i.__setitem__("run", "q"))),
        "stream/index.json run: 'q', expected 'r'",
    ),
    "audio-half-frame": (
        lambda run: (write("audio.pcm", b"\0" * 6)(run), write("audio.pcm.rate", b"32000\n")(run)),
        "audio.pcm: 6 bytes, expected whole 16-bit stereo frames",
    ),
    "audio-without-rate": (write("audio-oracle.pcm", b"\0" * 8), "audio-oracle.pcm.rate: "),
    "audio-rate-range": (
        lambda run: (write("audio.pcm", b"\0" * 8)(run), write("audio.pcm.rate", b"100\n")(run)),
        "audio.pcm.rate: 100, expected 8000..192000 Hz",
    ),
}


def test_a_whole_run_holds_field_by_field(tmp_path):
    run = make_run(tmp_path)
    lockstep.write_coverage(run)
    stream()(run)
    (run / "audio.pcm").write_bytes(bytes(8))
    (run / "audio.pcm.rate").write_text("32000\n")
    (run / "audio-oracle.pcm").write_bytes(b"")  # opened, no sound yet: no rate to name
    (run / "lockstep" / "replay" / "4").mkdir(parents=True)
    # trace.c's unframed line, as the real play 20261102T083234Z-pp-1p-match writes it (its line 91 was refused)
    calls(lambda ls: ls.insert(1, '{"k":"unframed","order":91,"pc":"0805a1c4"}'))(run)
    edit_json(run / "run.json", lambda r: r.__setitem__("oracle", {"lockstep": {"exit": 2, "error": "refused"}}))
    assert problems(run) == []


@pytest.mark.parametrize("name", sorted(CORRUPTIONS))
def test_each_corruption_is_refused_by_name(tmp_path, name):
    corrupt, expected = CORRUPTIONS[name]
    run = make_run(tmp_path)
    corrupt(run)
    found = problems(run)
    assert len(found) == 1 and found[0].startswith(expected), found
    assert lockstep.main(["check", str(run)]) == 1


def test_lockstep_result_agreeing_with_match_holds(tmp_path):
    run = make_run(tmp_path)
    first = json.loads((run / "lockstep" / "match.json").read_text())["first"]
    lockstep_result(matched_through_poll=-1, first=first)(run)
    assert problems(run) == []


def test_coverage_is_proved_from_the_run_folder_alone(tmp_path):
    """The join's function column hashes to the census sha256: a run copied away from its site still proves it."""
    run = make_run(tmp_path)
    lockstep.write_coverage(run)
    for path in (tmp_path / "runs" / "site").iterdir():
        path.unlink()
    record_ = json.loads((run / "run.json").read_text())
    assert lockstep.check_coverage(run, record_) == []
    names = "".join(n + "\n" for n in NAMES).encode()
    assert record_["census"]["sha256"] == hashlib.sha256(names).hexdigest()
