"""Source requirements, usable before any toolkit dependency is installed."""

import hashlib
import re
import subprocess
import tomllib
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


def sibling_checkouts(root: Path) -> dict[str, Path]:
    """Find tool checkouts by their declared distribution name, independent of directory spelling."""
    found: dict[str, Path] = {}
    for sibling in sorted(root.resolve().parent.iterdir()):
        project = sibling / "pyproject.toml"
        if sibling == root.resolve() or not (sibling / ".git").exists() or not project.is_file():
            continue
        name = tomllib.loads(project.read_text()).get("project", {}).get("name")
        if name in PACKAGES:
            if name in found:
                raise SourceError("sources.duplicate", f"{name}: multiple sibling checkouts: {found[name]}, {sibling}")
            found[name] = sibling
    return found


def checkout_key(root: Path, run: Run = run_command) -> str:
    """Hash HEAD and the complete dirty contents, including staged and untracked changes."""
    digest = hashlib.sha256()
    for args in (
        ["rev-parse", "HEAD"],
        ["diff", "--binary", "HEAD", "--"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ):
        code, output = run(["git", *args], root)
        if code:
            raise SourceError("sources.checkout", f"{root}: git {args[0]} failed: {output.strip()}")
        digest.update(output.encode())
        if args[0] == "ls-files":
            for name in output.split("\0"):
                if name:
                    path = root / name
                    digest.update(path.readlink().as_posix().encode() if path.is_symlink() else path.read_bytes())
    return digest.hexdigest()


def clean_requirements(dependencies: Sequence[str], run: Run = run_command, *, root: Path | None = None) -> list[str]:
    """Resolve tools from sibling checkouts when a workspace is supplied, otherwise from landed HEAD."""
    checkouts = sibling_checkouts(root) if root is not None else {}
    rows: list[str] = []
    landed: dict[str, str] = {}
    for row in dependencies:
        name = row.strip()
        if name in checkouts:
            row = str(checkouts[name])
        elif name in PACKAGES:
            if name not in landed:
                landed[name] = landed_commit(PACKAGES[name], run)
            row = f"{name} @ git+{PACKAGES[name]}@{landed[name]}"
        rows.append(row)
    return rows
