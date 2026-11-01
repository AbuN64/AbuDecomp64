import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import site_core, tas_serve
from abu_decomp.config import load_profile

OWNS_SEAT = True  # the runners leave tas_seat.check real for this module


class NoSeatTests(unittest.TestCase):
    """A server whose profile names no shared seat refuses a play at once; it never leaves one queued."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "game.z64").write_bytes(b"rom")
        (root / "movies").mkdir()
        profile = root / "seat.json"
        table = {"movies": "movies", "runs": "runs", "rom": "game.z64", "args": []}
        profile.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": table}))
        self.server = tas_serve.Server(load_profile(profile))

    def test_a_play_without_a_seat_is_refused_not_queued(self):
        with mock.patch.object(tas_serve.threading, "Thread") as thread:
            got = self.server.play("", 1)
        self.assertIn("no port executable", got["refused"])
        self.assertFalse(self.server.queued)
        thread.assert_not_called()

    def test_the_refusal_is_a_run_refused_event_with_the_reason(self):
        got = self.server.play("", 1)
        rows = [r for r in self.server.state()["runs"] if r["id"] == got["id"]]
        self.assertEqual(len(rows), 1)
        events = site_core.run_events(site_core.run_record(rows[0]))
        self.assertEqual(events[0]["kind"], "run.refused")
        self.assertEqual(events[0]["fields"]["reason"], got["refused"])

    def test_an_explicit_port_is_not_refused_by_the_seat(self):
        self.assertIsNone(self.server.seat_refusal("/some/candidate", "m", None))

    def test_the_corpus_gate_without_a_port_is_one_explicit_state(self):
        with mock.patch("builtins.print") as shown, mock.patch.object(tas_serve.time, "sleep") as sleep:
            self.server.gatekeeper()
        sleep.assert_not_called()
        (line,) = [call.args[0] for call in shown.call_args_list]
        self.assertIn("corpus gate not configured", line)


if __name__ == "__main__":
    unittest.main()
