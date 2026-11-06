"""`tas play --lockstep` takes the oracle half from the golden store (`taste oracle record`). Mocked: no process."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from abu_decomp import DecompError, tas_oracle

ANSWER = {"ok": True, "findings": [], "result": {"served": True, "path": "/store/x/lockstep.jsonl", "bytes": 9}}


def done(stdout, code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class StoredRecord(unittest.TestCase):
    oracle = MappingProxyType({"record": ["taste", "--profile", "p.json"], "lockstep_calls": 8})

    def run_record(self, answer, code=0):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(subprocess, "run") as run:
            run.return_value = done("log noise\n" + json.dumps(answer) + "\n", code)
            result = tas_oracle.stored_record(self.oracle, Path(folder), "on", 1, 300)
            return result, run.call_args.args[0], Path(folder)

    def test_asks_the_store_for_the_runs_movie_into_the_oracle_side(self):
        result, argv, run = self.run_record(ANSWER)
        self.assertEqual(result, ANSWER["result"])
        self.assertEqual(argv[:5], ["taste", "--profile", "p.json", "oracle", "record"])
        self.assertEqual(argv[argv.index("--movie") + 1], str(run / "movie.raw"))
        self.assertEqual(argv[argv.index("--into") + 1], str(run / "lockstep" / "oracle"))
        self.assertEqual(
            [argv[argv.index(flag) + 1] for flag in ("--pak", "--pads", "--exit-after", "--calls")],
            ["on", "1", "300", "8"],
        )

    def test_a_refusal_names_the_stores_finding(self):
        refusal = {"ok": False, "findings": [{"message": "the oracle exited 3"}], "result": None}
        with self.assertRaisesRegex(DecompError, "the oracle exited 3"):
            self.run_record(refusal, code=1)

    def test_unparsable_answer_is_refused(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(subprocess, "run") as run:
            run.return_value = done("", 0, "boom")
            with self.assertRaisesRegex(DecompError, "golden store refused"):
                tas_oracle.stored_record(self.oracle, Path(folder), "on", 1, 300)


if __name__ == "__main__":
    unittest.main()
