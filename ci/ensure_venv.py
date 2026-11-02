"""The repo-local virtual environment: made from pyproject.toml's dependencies (and the dev extra), keyed on a hash of
pyproject.toml. An unchanged tree reuses it with no install; a changed pyproject rebuilds it. Standard library only, so
any python3 of the required version can run it. Prints the environment's interpreter path on stdout (installer output
goes to stderr)."""

import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

VENV = ".venv"
STAMP = ".pyproject.sha256"


def key(root: Path) -> str:
    return hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest()


def requirements(root: Path) -> list[str]:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    return [*project["dependencies"], *project.get("optional-dependencies", {}).get("dev", [])]


def need_python(root: Path, version: tuple[int, int]) -> None:
    """Refuse an interpreter older than the pyproject's `requires-python = ">=X.Y"` before making an environment."""
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"].get("requires-python", "")
    found = re.fullmatch(r">=\s*(\d+)\.(\d+)", declared)
    if found and version < (int(found[1]), int(found[2])):
        raise SystemExit(f"bin/test: python3 is {version[0]}.{version[1]} but pyproject.toml needs {declared}")


def ensure(root: Path, run=subprocess.run, version: tuple[int, int] = sys.version_info[:2]) -> Path:
    """The interpreter of the environment for `root`'s pyproject.toml, made or rebuilt first when its stamp differs."""
    venv = root / VENV
    python = venv / "bin" / "python"
    stamp = venv / STAMP
    digest = key(root)
    if python.exists() and stamp.exists() and stamp.read_text() == digest:
        return python
    need_python(root, version)
    run([sys.executable, "-m", "venv", "--clear", str(venv)], check=True, stdout=sys.stderr)
    run([str(python), "-m", "pip", "install", "--quiet", *requirements(root)], check=True, stdout=sys.stderr)
    stamp.write_text(digest)  # last: a failed install leaves no stamp, so the next run installs again
    return python


def main() -> int:
    print(ensure(Path(__file__).resolve().parents[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
