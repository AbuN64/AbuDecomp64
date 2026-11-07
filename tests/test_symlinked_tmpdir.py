"""A path has one identity at every reader: the modules that build temp trees pass under a symlinked TMPDIR."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    "test_behave_tree",
    "test_fn_io",
    "test_ship_proof",
    "test_tas_serve",
    "test_tas_seat",
    "test_tas_timelapse",
    "test_behave_candidate",
    "test_mcp",
    "test_ensure_venv",
    "test_tas_select",
]


def test_the_temp_tree_modules_pass_under_a_symlinked_tmpdir():
    with tempfile.TemporaryDirectory() as tmp:
        real = Path(tmp).resolve() / "real"
        real.mkdir()
        link = Path(tmp).resolve() / "link"
        link.symlink_to(real)
        src = f"{ROOT / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"
        env = dict(os.environ, TMPDIR=str(link), PYTHONPATH=src)
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *(f"tests/{m}.py" for m in MODULES)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1000:]
