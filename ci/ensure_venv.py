"""Build the repo-local venv from declared requirements. Sibling tool checkouts take precedence over landed
commits. Its stamp includes pyproject, resolved requirements, and each checkout's HEAD and dirty contents.
Installer output goes to stderr; stdout names the interpreter. Standard library only for clean bootstrap."""

import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

VENV = ".venv"
STAMP = ".pyproject.sha256"


def requirements(root: Path, run=None) -> list[str]:
    """Resolve declared dependencies and dev extras to sibling checkouts or landed commits."""
    sys.path.insert(0, str(root / "src"))
    try:
        from abu_decomp import sources
    finally:
        sys.path.pop(0)
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    dependencies = sources.clean_requirements(
        [*project["dependencies"], *project.get("optional-dependencies", {}).get("dev", [])],
        run or sources.run_command,
        root=root,
    )
    return dependencies


def key(root: Path, rows: list[str], run=None) -> str:
    from abu_decomp import sources

    digest = hashlib.sha256((root / "pyproject.toml").read_bytes() + "\n".join(rows).encode())
    for row in rows:
        if Path(row).is_absolute():
            digest.update(sources.checkout_key(Path(row), run or sources.run_command).encode())
    return digest.hexdigest()


def need_python(root: Path, version: tuple[int, int]) -> None:
    """Refuse an interpreter older than the pyproject's `requires-python = ">=X.Y"` before making an environment."""
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"].get("requires-python", "")
    found = re.fullmatch(r">=\s*(\d+)\.(\d+)", declared)
    if found and version < (int(found[1]), int(found[2])):
        raise SystemExit(f"bin/test: python3 is {version[0]}.{version[1]} but pyproject.toml needs {declared}")


def ensure(root: Path, run=subprocess.run, version: tuple[int, int] = sys.version_info[:2], lookup=None) -> Path:
    """The interpreter of the environment for `root`'s pyproject.toml, made or rebuilt first when its stamp differs."""
    venv = root / VENV
    python = venv / "bin" / "python"
    stamp = venv / STAMP
    rows = requirements(root, lookup)
    digest = key(root, rows, lookup)
    if python.exists() and stamp.exists() and stamp.read_text() == digest:
        return python
    need_python(root, version)
    run([sys.executable, "-m", "venv", "--clear", str(venv)], check=True, stdout=sys.stderr)
    run([str(python), "-m", "pip", "install", "--quiet", *rows], check=True, stdout=sys.stderr)
    stamp.write_text(digest)  # last: a failed install leaves no stamp, so the next run installs again
    return python


def main() -> int:
    print(ensure(Path(__file__).resolve().parents[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
