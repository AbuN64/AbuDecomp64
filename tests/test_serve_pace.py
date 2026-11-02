"""serve_pace: the one home of live wall-clock pacing."""

import socket
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from abu_decomp import serve_pace


class WaitForFilesTest(unittest.TestCase):
    def test_returns_when_the_runs_own_write_creates_the_files(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            files = [run / "a.pcm", run / "a.pcm.rate"]
            ours, theirs = socket.socketpair()
            self.addCleanup(ours.close)
            self.addCleanup(theirs.close)

            def write():
                for path in files:
                    path.write_text("x")

            threading.Timer(0.1, write).start()
            self.assertTrue(serve_pace.wait_for_files(run, files, lambda: True, ours))

    def test_false_when_the_run_is_gone_or_the_client_left(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            ours, theirs = socket.socketpair()
            self.addCleanup(ours.close)
            self.assertFalse(serve_pace.wait_for_files(run, [run / "a"], lambda: False, ours))
            threading.Timer(0.1, theirs.close).start()
            self.assertFalse(serve_pace.wait_for_files(run, [run / "a"], lambda: True, ours))


class BeatTest(unittest.TestCase):
    def test_wait_sleeps_to_the_next_due_time_and_never_into_the_past(self):
        with mock.patch.object(serve_pace, "clock", side_effect=[10.0, 10.0, 10.5]), mock.patch.object(
            serve_pace.time, "sleep"
        ) as sleep:
            beat = serve_pace.Beat(1.0)  # due 10.0
            beat.wait()  # now 10.0 -> due 11.0
            sleep.assert_called_with(1.0)
            beat.wait(advance=False, floor=0.002)  # now 10.5 -> still due 11.0
            self.assertAlmostEqual(sleep.call_args.args[0], 0.5)

    def test_allowance_counts_the_lead_and_the_elapsed_time(self):
        with mock.patch.object(serve_pace, "clock", side_effect=[5.0, 6.0]):
            self.assertEqual(serve_pace.Allowance(1000, 0.25).frames(), 1250)


if __name__ == "__main__":
    unittest.main()
