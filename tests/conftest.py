"""Test repos start from git with no host template: a host init.templatedir carries commit-msg and pre-push hooks
(subject <= 72, remote tip known locally) that refuse the throwaway repos the tests build.

Every test also gets its own runtime file (ABU_RUNTIME, so its slot pool is its own runtime.cache.dir) and no real
gcovr: a test must never queue behind (or hold a slot of) the live rig pool that the lanes build through, nor read the
host's ~/.config/abu/runtime.toml."""

import atexit
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
# The host's free disk and memory are stand-ins here and in every python a test starts (see host_seam/sitecustomize.py).
os.environ["PYTHONPATH"] = os.pathsep.join(
    filter(None, [str(_HERE / "host_seam"), str(_HERE.parent / "src"), os.environ.get("PYTHONPATH")])
)
runpy.run_path(str(_HERE / "host_seam/sitecustomize.py"))
import standin_runtime  # noqa: E402  (found through host_seam, which sitecustomize.py put on sys.path)

_EMPTY = tempfile.mkdtemp(prefix="git-template-")
os.environ["GIT_TEMPLATE_DIR"] = _EMPTY
atexit.register(shutil.rmtree, _EMPTY, ignore_errors=True)

_REAL_RUN = subprocess.run


def _run_without_gcovr(command, *args, **kwargs):
    """gcovr is a real, minutes-long process: no unit test runs it (a test that checks the call mocks run itself)."""
    if isinstance(command, (list, tuple)) and "gcovr" in command and command[:1] == [sys.executable]:
        return subprocess.CompletedProcess(command, 0, "", "")
    return _REAL_RUN(command, *args, **kwargs)


@pytest.fixture(autouse=True)
def _stand_in_seat(request, monkeypatch):
    """A play without --port needs a shared seat whose build states the project's current main (tas_seat); tests that
    drive play with a stand-in port skip that check; a test module that sets OWNS_SEAT = True runs it."""
    if not getattr(request.module, "OWNS_SEAT", False):
        from abu_decomp import tas_seat

        monkeypatch.setattr(tas_seat, "check", lambda cfg: None)


@pytest.fixture(autouse=True)
def _isolated_rig(tmp_path, monkeypatch):
    from abucache import runtime, slots

    stand_in = standin_runtime.write(tmp_path)
    monkeypatch.setenv("ABU_RUNTIME", str(stand_in))
    monkeypatch.setattr(slots, "default", slots.Slots(runtime.load(stand_in)))
    monkeypatch.setattr(subprocess, "run", _run_without_gcovr)
