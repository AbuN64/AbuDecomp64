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

    def test_a_mask_reaches_the_store_and_no_mask_adds_no_flag(self):
        _, argv, _ = self.run_record(ANSWER)
        self.assertNotIn("--mask", argv)
        self.oracle = MappingProxyType({**self.oracle, "lockstep_mask": "800e9400-800ebc00"})
        _, argv, _ = self.run_record(ANSWER)
        self.assertEqual(argv[argv.index("--mask") + 1], "800e9400-800ebc00")


class LockstepMask(unittest.TestCase):
    def test_the_mask_file_becomes_one_spec_for_both_recorders(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mask.tsv"
            path.write_text("# comment\n\n0x800E9400-0x800EBC00\tguest stack\n0x8011f660-0x8011f664\tOSThread state\n")
            spec = tas_oracle.mask_spec(path)
            self.assertEqual(spec, "800e9400-800ebc00,8011f660-8011f664")
            self.assertEqual(tas_oracle.mask_env(spec), {"N64C_LOCKSTEP_MASK": spec, "N64R_LOCKSTEP_MASK": spec})
            self.assertEqual(tas_oracle.mask_env(""), {})
            env = tas_oracle.lockstep_env({"lockstep": True, "lockstep_calls": 8, "lockstep_mask": spec}, Path(folder))
            self.assertEqual(env["N64R_LOCKSTEP_MASK"], spec)
            self.assertNotIn(
                "N64R_LOCKSTEP_MASK", tas_oracle.lockstep_env({"lockstep": True, "lockstep_calls": 8}, Path(folder))
            )
            for bad in (
                "0x800E9400-0x800EBC00\n",
                "0x800E9401-0x800EBC00\tx\n",
                "0x10-0x8\tx\n",
                "800E9400-800EBC00\tx\n",
            ):
                path.write_text(bad)
                with self.assertRaises(DecompError, msg=bad):
                    tas_oracle.mask_spec(path)

    def test_a_dump_replay_masks_what_its_record_masked(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            self.assertEqual(tas_oracle.recorded_mask(run), "")
            side = tas_oracle.lockstep_side(run, "port")
            side.mkdir(parents=True, exist_ok=True)
            (side / "lockstep.jsonl").write_text(json.dumps({"k": "meta", "mask": "0-4"}) + "\n{}\n")
            self.assertEqual(tas_oracle.recorded_mask(run), "0-4")


if __name__ == "__main__":
    unittest.main()
