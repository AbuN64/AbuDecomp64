import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from abu_decomp import DecompError, site_core, tas, tas_serve
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
        with mock.patch("builtins.print") as shown, mock.patch.object(self.server, "settled") as settled:
            self.server.gatekeeper()
        settled.assert_not_called()
        (line,) = [call.args[0] for call in shown.call_args_list]
        self.assertIn("corpus gate not configured", line)


class StartFailureTests(unittest.TestCase):
    """A play whose start thread fails before the run writes its record is refused with the reason, never left
    queued and never forgotten (live: tas.play raised the unreadable-build-graph DecompError, the thread ate it)."""

    REASON = "5 *.failed markers but none of the build files naming the port's units is readable: /seat/build.ninja"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "game.z64").write_bytes(b"rom")
        (root / "movies").mkdir()
        (root / "port").write_bytes(b"bin")
        profile = root / "seat.json"
        table = {"movies": "movies", "runs": "runs", "rom": "game.z64", "args": [], "port": "port"}
        profile.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": table}))
        self.server = tas_serve.Server(load_profile(profile))
        for target in ("abu_decomp.tas_seat.check", "abu_decomp.tas.reconcile_runs"):
            patch = mock.patch(target)
            patch.start()
            self.addCleanup(patch.stop)

    def start(self, error):
        class Now:  # the start thread runs where the test can see its end
            def __init__(self, target, daemon=False, name=""):
                self.target, self.name = target, name

            def start(self):
                if not self.name.startswith("reach-"):  # the reach relay is not under test
                    self.target()

        with (
            mock.patch.object(tas_serve.threading, "Thread", Now),
            mock.patch("abu_decomp.tas.play", side_effect=error),
            mock.patch("builtins.print") as shown,
        ):
            got = self.server.play("", 1)
        return got, shown

    def test_a_start_that_fails_is_a_refused_row_with_its_reason(self):
        got, shown = self.start(DecompError("tas.health", self.REASON))
        self.assertNotIn(got["id"], self.server.queued)
        rows = [r for r in self.server.state()["runs"] if r["id"] == got["id"]]
        self.assertEqual([r["status"] for r in rows], ["done"])
        events = site_core.run_events(site_core.run_record(rows[0]))
        self.assertEqual(events[0]["kind"], "run.refused")
        self.assertEqual(events[0]["fields"]["reason"], self.REASON)
        self.assertIn(self.REASON, shown.call_args.args[0])

    def test_a_failure_that_is_not_a_decomp_error_names_its_type(self):
        got, _ = self.start(OSError("disk full"))
        self.assertEqual(self.server.refused[got["id"]]["error"], "OSError: disk full")

    def test_stop_of_a_refused_play_answers_that_it_was_refused(self):
        got, _ = self.start(DecompError("tas.health", self.REASON))
        self.assertEqual(self.server.stop(got["id"]), {"id": got["id"], "stopped": False, "refused": self.REASON})
        with self.assertRaises(DecompError):
            self.server.stop("never-existed")


class SeatFlipTests(unittest.TestCase):
    """The shared seat is a live link the port deploy flips to each landed build; a site started before a flip plays
    the build the link names now (live 11-01 10:51:54Z: it kept port.74ec472 and refused every play as stale)."""

    def test_a_play_checks_the_build_the_link_names_now_not_the_one_at_start(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "game.z64").write_bytes(b"rom")
        (root / "movies").mkdir()
        for build in ("port.old", "port.new"):
            (root / build).mkdir()
            (root / build / "ragewars").write_bytes(build.encode())
        (root / "port").symlink_to(root / "port.old")
        profile = root / "seat.json"
        table = {"movies": "movies", "runs": "runs", "rom": "game.z64", "args": [], "port": "port/ragewars"}
        profile.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": table}))
        server = tas_serve.Server(load_profile(profile))
        self.assertEqual(server.cfg["port"], root / "port.old" / "ragewars")
        (root / "port").unlink()
        (root / "port").symlink_to(root / "port.new")
        checked = []
        with mock.patch("abu_decomp.tas_seat.check", side_effect=lambda cfg: checked.append(cfg["port"])):
            self.assertIsNone(server.seat_refusal(None, "m", None))
        self.assertEqual(checked, [root / "port.new" / "ragewars"])
        self.assertEqual(server.cfg["port"], root / "port.new" / "ragewars")


class PortExitTests(unittest.TestCase):
    """A port that exits before its first poll ends as run.refused with the reason (live 11-01 10:12:10Z: the seat held
    no shader archive, the port printed so and exited 4 at once; the feed said only run.ended outcome exit, poll 0)."""

    OUTPUT = (
        "boot-config: com_expansionPak=1 rdram_bytes=0x00800000\n"
        "ragewars: the renderer's shader archive is missing (/seat/fast3d-shaders.o2r); reinstall the game\n"
    )
    EXITED: ClassVar[dict[str, object]] = {"outcome": "exit", "subject": "exit 4", "polls_reached": 0}

    def test_the_reason_is_the_exit_and_the_last_line_the_port_printed(self):
        reason = tas.no_first_frame(4, self.OUTPUT, self.EXITED, False)
        self.assertIn("(exit 4)", reason)
        self.assertTrue(reason.endswith("shader archive is missing (/seat/fast3d-shaders.o2r); reinstall the game"))

    def test_a_port_that_printed_nothing_says_so(self):
        self.assertIn("it printed nothing", tas.no_first_frame(4, "\n", self.EXITED, False))

    def test_a_stop_a_clean_finish_or_a_run_that_polled_is_not_a_refusal(self):
        self.assertIsNone(tas.no_first_frame(0, self.OUTPUT, self.EXITED, True))
        self.assertIsNone(tas.no_first_frame(0, self.OUTPUT, {**self.EXITED, "outcome": "ok"}, False))
        self.assertIsNone(tas.no_first_frame(4, self.OUTPUT, {**self.EXITED, "polls_reached": 3}, False))

    def test_the_record_error_reaches_the_feed_as_run_refused(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "game.z64").write_bytes(b"rom")
        (root / "movies").mkdir()
        profile = root / "seat.json"
        table = {"movies": "movies", "runs": "runs", "rom": "game.z64", "args": []}
        profile.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": table}))
        server = tas_serve.Server(load_profile(profile))
        run = root / "runs" / "plays" / "20261101T101210Z-play-20261101-101210"
        run.mkdir(parents=True)
        reason = tas.no_first_frame(4, self.OUTPUT, self.EXITED, False)
        record = {**self.EXITED, "movie": "play-20261101-101210", "status": "done", "ended": 2.0, "error": reason}
        events = site_core.run_events(site_core.run_record({**server.row(run, record), "started": 1.0}))
        self.assertEqual([e["kind"] for e in events], ["run.refused"])
        self.assertEqual(events[0]["fields"]["reason"], reason)


if __name__ == "__main__":
    unittest.main()
