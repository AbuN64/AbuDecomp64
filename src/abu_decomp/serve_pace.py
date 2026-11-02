"""How a run is paced and who drives it, and the one wall-clock pacing home of the live streams: shared by the run
rows (tas_serve), the audio route and the video routes, and the serve's worker pool. Nothing else reads the
monotonic clock for live pacing."""

from __future__ import annotations

import select
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from abu_decomp import inotify, tas

RELEASED = "released"  # in a taken run's folder once given back: the run stays a take, live input drives it no more


def driven(run: Path) -> bool:
    """Whether live input drives the run now: taken and not given back since."""
    return (run / tas.TAKEN).exists() and not (run / RELEASED).exists()


def realtime(run: Path, record: dict[str, Any]) -> bool:
    """The actual pacing, including a turbo prefix and a live takeover, shared by state and audio."""
    if record.get("speed") == 1:
        return True
    # TAKEN is written before a retry's prefix starts. Only the port's acknowledgement means 1x.
    if not driven(run):
        return False
    try:
        return "live input took over at poll " in (run / "output.txt").read_text(errors="replace")
    except OSError:
        return False


def worker_pool(width: int) -> ThreadPoolExecutor:
    """The serve's background workers (plays started from a request), `width` at a time."""
    return ThreadPoolExecutor(max_workers=max(1, width))


def clock() -> float:
    """The wall clock live pacing runs on (monotonic seconds)."""
    return time.monotonic()


class Beat:
    """A fixed-rate beat: `wait` sleeps to the next due time; a round's own work does not slow the pace."""

    def __init__(self, period: float) -> None:
        self.period = period
        self.due = clock()

    def wait(self, advance: bool = True, floor: float = 0.0) -> None:
        """Advance the due time one period (never into the past) when `advance`, then sleep to it, at least `floor`."""
        current = clock()
        if advance:
            self.due = max(self.due + self.period, current)
        time.sleep(max(floor, self.due - current))


class Allowance:
    """How many sample frames a wall-paced stream may have sent by now: `rate` a second plus a lead of `lead` s."""

    def __init__(self, rate: int, lead: float) -> None:
        self.rate, self.lead, self.began = rate, lead, clock()

    def frames(self) -> int:
        return int((clock() - self.began + self.lead) * self.rate)


def wait_for_files(run: Path, files: list[Path], alive: Callable[[], bool], client: Any) -> bool:
    """Block until every file exists. The run's own writes into its folder wake it; no duration bounds it.
    False when the run is no longer live or the client closed its end."""
    watcher = inotify.Watcher()
    try:
        watcher.add(run, inotify.CREATE | inotify.MOVED_TO | inotify.CLOSE_WRITE | inotify.DELETE)
        while True:
            if all(path.is_file() for path in files):
                return True
            if not alive():
                return False
            ready = select.select([watcher.fd, client], [], [])[0]
            if client in ready:
                return False
            watcher.read()
    finally:
        watcher.close()
