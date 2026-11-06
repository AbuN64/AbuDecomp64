"""RageWarsEnhanced owns the n64 shim (`shim/`); this checkout reads the copy found beside it.

The sibling is found the way CakeTaste64 finds RageWarsDecomp: the parent of this repository's git common directory
(the registered checkout, also from a worktree) holds `RageWarsEnhanced`. No environment variable, no hand path."""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import DecompError

SIBLING = "RageWarsEnhanced"
REPO = Path(__file__).resolve().parents[2]


def sibling(repo: Path = REPO, run: Callable[..., Any] = subprocess.run) -> Path:
    """The RageWarsEnhanced checkout beside `repo`'s registered checkout; refused naming the path to clone."""
    done = run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise DecompError(
            "enhanced.repo",
            f"{repo} is no git checkout, so RageWarsEnhanced cannot be found beside it",
        )
    path = Path(done.stdout.strip()).parent.parent / SIBLING
    if not path.is_dir():
        raise DecompError(
            "enhanced.missing",
            f"RageWarsEnhanced owns the shim and is not beside this repository: clone it to {path}",
            f"git clone RageWarsEnhanced to {path}",
        )
    return path


def shim_dir(repo: Path = REPO, run: Callable[..., Any] = subprocess.run) -> Path:
    """RageWarsEnhanced's `shim/`, the only copy of the shim."""
    path = sibling(repo, run) / "shim"
    if not path.is_dir():
        raise DecompError("enhanced.shim", f"{path} is missing: update the RageWarsEnhanced checkout")
    return path
