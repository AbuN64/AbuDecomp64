"""Source requirements, usable before any toolkit dependency is installed."""

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from abu_decomp import DecompError

DECOMP_URL = "https://github.com/AbuN64/abu-decomp-64"
# Distribution name -> its repository: the packages a deploy can install, each by its own commit.
PACKAGES = {
    "abucache": "https://github.com/AbuN64/cache-64",
    "abu-decomp-64": DECOMP_URL,
    "unbake": "https://github.com/AbuN64/cake-unbake-64",
    "recipemaker": "https://github.com/AbuN64/recipe-maker-64",
    "cake-taste-64": "https://github.com/AbuN64/cake-taste-64",
}
_HEAD = re.compile(r"^([0-9a-f]{40})\tHEAD$", re.MULTILINE)
Run = Callable[..., tuple[int, str]]


class SourceError(DecompError):
    pass


def run_command(argv: Sequence[str], cwd: Path | None = None) -> tuple[int, str]:
    done = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout + done.stderr


def landed_commit(url: str, run: Run = run_command) -> str:
    """The commit a tool's source repository is at now (its HEAD), the one place that says where a tool has landed."""
    code, output = run(["git", "ls-remote", url, "HEAD"], None)
    found = _HEAD.search(output)
    if code or not found:
        raise SourceError("deploy.landed", f"{url}: git ls-remote HEAD failed: {output.strip()[-300:] or 'no HEAD'}")
    return found[1]


def clean_requirements(dependencies: Sequence[str], run: Run = run_command) -> list[str]:
    """`dependencies` for a clean install: each bare name of our tools becomes `name @ git+URL@landed commit`, so pip
    can fetch it; every other row stays as written. The repository pins no tool by commit (a pin drifts each time a
    tool lands and strands the next deploy), so a tool is only ever named."""
    rows: list[str] = []
    landed: dict[str, str] = {}
    for row in dependencies:
        name = row.strip()
        if name in PACKAGES:
            if name not in landed:
                landed[name] = landed_commit(PACKAGES[name], run)
            row = f"{name} @ git+{PACKAGES[name]}@{landed[name]}"
        rows.append(row)
    return rows
