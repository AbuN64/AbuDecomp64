# SPDX-License-Identifier: GPL-3.0-or-later
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abu_decomp import DecompError, tas_entries
from abu_decomp.config import Profile

FIXTURE = Path(__file__).parent / "fixtures/entries"
FNS = ["Sound_GetSampleDmaWindowAddress", "Sound_GetCachedAudioBlockAddress"]


class EntriesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text('{"bindings": {}}')

    def test_captured_wl3_slice(self):
        result = tas_entries.compare(
            FIXTURE / "oracle", FIXTURE / "port", (FIXTURE / "nm").read_text(), self.manifest, FNS, 4800, 4802
        )
        self.assertEqual(result["exit"], 1)
        self.assertEqual(result["differing"], 3)
        self.assertEqual([r["retrace"] for r in result["rows"]], [4800, 4801, 4802])
        self.assertEqual(
            result["totals"],
            {
                FNS[0]: {"oracle": 54, "port": 55},
                FNS[1]: {"oracle": 63, "port": 55},
            },
        )

    def test_bindings_aliases_notes_and_tsv(self):
        self.manifest.write_text(json.dumps({"bindings": {"native": FNS[0] + "_recomp"}}))
        o, p = self.root / "o", self.root / "p"
        o.write_text(f"4 thread {FNS[0]} 9\n4 thread {FNS[1]} 10\n")
        p.write_text("4 thread 1 100 9\n4 thread 1 200 10\n4 thread 0 sc:ev:VI 10\n")
        result = tas_entries.compare(o, p, f"100 T native\n200 W addrLow_{FNS[1]}_de\n", self.manifest, FNS, 4, 4)
        self.assertEqual(result["exit"], 0)
        self.assertEqual(result["rows"][0]["oracle_order"], FNS)
        tas_entries.write_tsv(self.root / "out.tsv", result, FNS)
        self.assertIn(f"{FNS[0]}*1 {FNS[1]}*1", (self.root / "out.tsv").read_text())
        with self.assertRaises(DecompError):
            tas_entries.compare(o, p, "", self.manifest, FNS, 4, 5)

    def test_missing_and_empty_sides_refuse(self):
        for side in ("oracle", "port"):
            o, p = self.root / "oracle", self.root / "port"
            o.write_text(f"4 t {FNS[0]}\n")
            p.write_text("4 t 1 100 0\n")
            missing = o if side == "oracle" else p
            missing.unlink()
            for empty in (False, True):
                if empty:
                    missing.touch()
                with self.assertRaisesRegex(DecompError, side + " entry trace is missing or empty"):
                    tas_entries.compare(o, p, "", self.manifest, FNS, 4, 4)

    @patch("abu_decomp.tas_entries.tas.play")
    def test_bake_refusal_is_preserved(self, play):
        text = "tas bake --what movie --movie monkey-1-3000: no coverage table yet: run decomp tas coverage first"
        play.side_effect = DecompError("tas.bake", text)
        port = self.root / "exe"
        port.touch()
        profile = Profile("test", self.root / "profile", "digest", {"tas": {"oracle": {"host": "oracle"}}})
        with self.assertRaisesRegex(DecompError, "no coverage table yet"):
            tas_entries.run(profile, "monkey-1-3000", "4800:4895", FNS[0], port, self.manifest, self.root / "pair")

    def test_windows(self):
        self.assertEqual(tas_entries.window("0:2"), (0, 2))
        for value in ("3:2", "-1:2", "1", "1:2:3", "a:3"):
            with self.assertRaises(DecompError):
                tas_entries.window(value)

    @patch("abu_decomp.tas_entries.subprocess.run")
    @patch("abu_decomp.tas_entries.tas.play")
    def test_public_runner_uses_private_profile(self, play, nm):
        table = {"oracle": {"host": "oracle"}, "env": {"keep": "value"}, "runs": "shared"}
        profile = Profile("test", self.root / "profile.json", "digest", {"tas": table})
        port = self.root / "exe"
        port.touch()
        out = self.root / "pair"

        def replay(actual, movie, **kwargs):
            config = actual.section("tas")
            self.assertEqual(movie, "movie")
            self.assertTrue(kwargs["oracle"])
            self.assertFalse(kwargs["state_diff"])
            self.assertEqual(config["env"]["keep"], "value")
            self.assertEqual(config["env"]["N64C_ENTRY_TRACE"], f"{out}/port.entries:4:4")
            self.assertEqual(config["oracle"]["env"]["N64R_TRACE"], f"{out}/oracle.entries")
            (out / "oracle.entries").write_text(f"4 t {FNS[0]}\n")
            (out / "port.entries").write_text("4 t 1 100 0\n")
            return {"outcome": "ok", "oracle": {"outcome": "ok"}}

        play.side_effect = replay
        nm.return_value.stdout = f"100 T {FNS[0]}\n"
        result = tas_entries.run(profile, "movie", "4:4", FNS[0], port, self.manifest, out)
        self.assertEqual(result["exit"], 0)
        self.assertTrue((out / "entries.tsv").is_file())
        self.assertEqual(profile.section("tas"), table)
        with self.assertRaises(FileExistsError):
            tas_entries.run(profile, "movie", "4:4", FNS[0], port, self.manifest, out)
