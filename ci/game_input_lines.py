"""What the game proof reads from the game's WAYLAND_DEBUG stream.

libwayland prints `wl_keyboard#N` (older builds `@N`), so both id forms are accepted.
"""

import re

KEY = re.compile(r"wl_keyboard[@#]\d+\.key\(\d+, \d+, (\d+), (\d)\)")
ENTER = re.compile(r"wl_keyboard[@#]\d+\.enter\(")


def key_event(line: str) -> tuple[int, int] | None:
    """The (evdev code, 1 press / 0 release) a wl_keyboard.key line delivers, else None."""
    match = KEY.search(line)
    return (int(match[1]), int(match[2])) if match else None


def entered(line: str) -> bool:
    """Whether the line is the compositor giving the game's window keyboard focus."""
    return ENTER.search(line) is not None
