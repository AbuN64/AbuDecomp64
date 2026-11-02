"""One game on a fresh display: the launch path of `decomp display invoke` (the site watches runs on their data
path, `/watch` -> `/#/play/RUN`, and keeps no display of its own)."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

Game = Callable[[Path], tuple[list[str], dict[str, str]]]


def invoke(
    shown: Any,
    size: str,
    game: Game,
    started: Callable[[str, list[str]], None],
    run: Callable[..., subprocess.CompletedProcess] | None = None,
) -> dict[str, Any]:
    """Run `game(folder)` on a fresh display, call `started(identity, argv)` once it runs, delete the display when the
    game exits and answer {display, exit}. The one launch path of `decomp display invoke`."""
    record = shown.create(size)
    try:
        command, env = game(Path(record.directory) / "game")
        started(record.identity, command)
        code = (run or subprocess.run)(command, env=env | shown.environment(record.identity), check=False).returncode
    finally:
        shown.delete(record.identity)
    return {"display": record.identity, "exit": code}
