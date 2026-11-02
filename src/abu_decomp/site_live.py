"""The site's own live game: one display tas serve starts and keeps, running the profile's game (`tas.live`).

`tas.serve.live` names the display size (`"1280x720"`). The keeper creates a display, runs the game on it the way
`decomp display invoke` does with no command, deletes the display when the game exits and starts the next one at
once after a clean exit (the owner quit from the menu). A crash stops the keeper and `/watch` names it, never a
silent restart loop. `/watch` answers a redirect to `/viewer/<id>` of the running display: the home page's one click.
"""

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
    game exits and answer {display, exit}. The one launch path of `decomp display invoke` and the site's keeper."""
    record = shown.create(size)
    try:
        command, env = game(Path(record.directory) / "game")
        started(record.identity, command)
        code = (run or subprocess.run)(command, env=env | shown.environment(record.identity), check=False).returncode
    finally:
        shown.delete(record.identity)
    return {"display": record.identity, "exit": code}


class Keeper:
    """Keeps one live display for the site; `watch()` names its viewer path or refuses with the reason it has none."""

    def __init__(
        self, backend: Callable[[], Any], size: str | None, game: Game, run: Callable[..., Any] | None = None
    ) -> None:
        self.backend, self.size, self.game, self.run_game = backend, size, game, run
        self.current: str | None = None
        self.fault: str | None = None
        self.lock = threading.Lock()

    def _started(self, identity: str, _argv: list[str]) -> None:
        with self.lock:
            self.current = identity

    def run(self) -> None:
        """Start the game, and again after each clean exit; a crash or a display that cannot start ends the loop."""
        while self.size:
            try:
                answer = invoke(self.backend(), self.size, self.game, self._started, self.run_game)
            except DecompError as error:
                fault = f"the live display could not start: {error}"
            else:
                fault = None if answer["exit"] == 0 else f"the live game on {answer['display']} exited {answer['exit']}"
            with self.lock:
                self.current, self.fault = None, fault
            if fault:
                return

    def start(self) -> None:
        if self.size:
            threading.Thread(target=self.run, daemon=True, name="site-live").start()

    def watch(self) -> str:
        with self.lock:
            current, fault = self.current, self.fault
        if current:
            return f"/viewer/{current}"
        if not self.size:
            raise DecompError("site.live.notfound", "the profile names no tas.serve.live display size, so no live game")
        raise DecompError("site.live.notfound", fault or "the live game is starting: reload in a moment")
