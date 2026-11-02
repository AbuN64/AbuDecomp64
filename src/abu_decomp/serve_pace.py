"""How a run is paced and who drives it: shared by the run rows (tas_serve) and the audio route."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from abu_decomp import tas

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
