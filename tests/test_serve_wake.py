"""serve_wake and inotify.Timer: streams block on file events, the peer closing and a kernel timer, never a sleep."""

import socket
import tempfile
import threading
import unittest
from pathlib import Path

from abu_decomp import inotify, serve_wake


class WakeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.server_end, self.client = socket.socketpair()
        self.addCleanup(self.server_end.close)
        self.addCleanup(self.client.close)

    def test_the_first_wait_returns_at_once_then_only_a_named_file_wakes_it(self):
        with serve_wake.Wake(self.server_end) as wake:
            wake.watch(self.dir, {"view.bmp"})
            self.assertTrue(wake.wait())
            (self.dir / "other").write_text("x")  # not named: no wake
            threading.Timer(0, lambda: (self.dir / "view.bmp").write_text("x")).start()
            self.assertTrue(wake.wait())

    def test_a_signal_pipe_wakes_the_wait_with_no_file_change(self):
        import os

        read, write = os.pipe2(os.O_NONBLOCK)
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        with serve_wake.Wake(self.server_end) as wake:
            wake.signal(read)
            self.assertTrue(wake.wait())
            threading.Timer(0, lambda: os.write(write, b"x")).start()
            self.assertTrue(wake.wait())
            self.assertEqual(wake.signals, [read])

    def test_the_peer_closing_ends_the_wait(self):
        with serve_wake.Wake(self.server_end) as wake:
            wake.watch(self.dir, {"view.bmp"})
            self.assertTrue(wake.wait())
            self.client.close()
            self.assertFalse(wake.wait())

    def test_bytes_from_the_peer_are_dropped_and_do_not_end_the_wait(self):
        with serve_wake.Wake(self.server_end) as wake:
            wake.watch(self.dir, {"view.bmp"})
            wake.wait()
            self.client.send(b"stray")
            threading.Timer(0.0, lambda: (self.dir / "view.bmp").write_text("x")).start()
            self.assertTrue(wake.wait())

    def test_a_change_inside_the_gap_waits_for_the_timer(self):
        with serve_wake.Wake(self.server_end, gap=0.05) as wake:
            wake.watch(self.dir, None)
            self.assertTrue(wake.wait())
            (self.dir / "a").write_text("1")  # already seen when the second wait starts
            self.assertTrue(wake.held)
            self.assertTrue(wake.wait())
            self.assertTrue(wake.held)  # re-armed: the next wake is a gap away again

    def test_watching_and_dropping_directories(self):
        sub = self.dir / "run"
        sub.mkdir()
        with serve_wake.Wake(self.server_end) as wake:
            wake.watch(self.dir, None)
            wake.watch(sub, {"x"})
            self.assertEqual(wake.watching(), {self.dir, sub})
            wake.drop(sub)
            self.assertEqual(wake.watching(), {self.dir})


class TimerTests(unittest.TestCase):
    def test_a_timer_is_readable_once_it_goes_off(self):
        import select

        timer = inotify.Timer()
        self.addCleanup(timer.close)
        self.assertFalse(timer.expired())
        timer.arm(0.01)
        self.assertEqual(select.select([timer.fd], [], [])[0], [timer.fd])
        self.assertTrue(timer.expired())
        self.assertFalse(timer.expired())


if __name__ == "__main__":
    unittest.main()
