"""A stand-in for the host's runtime file (AbuCache's own format, every key required) for the unit suite.

The slot pool lives under `runtime.cache.dir` and a process reads the file `ABU_RUNTIME` names, else the operator's
`~/.config/abu/runtime.toml`. A test must read neither the host's file nor the host's pool: `install` writes a file of
its own under a directory of its own and points `ABU_RUNTIME` at it, for this process and every python it starts."""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

PREFIX = "abu-runtime-stand-in-"
NAME = "stand-in-runtime.toml"  # the name that marks a file as this suite's own
STAND_IN_GIB = 1024.0  # the free memory sitecustomize.py stands in for

TEXT = """[resources]
cores = {cpus}
workers = {cpus}
worker_memory = {worker_memory}
parent_memory = 0
reserve_memory = 0

[capacities]

[cache]
dir = "{cache}"
bytes = {cache_bytes}
keep_seconds = 3600

[stages]
log_bytes = 65536

[events]
feed = "{feed}"
slow_seconds = 60
overrun_percent = 150
"""


def write(directory: Path) -> Path:
    """`directory/stand-in-runtime.toml`: one worker per CPU this process may use, each given its share of the
    stand-in memory (no memory figure of its own), and a cache and feed inside `directory`."""
    cpus = len(os.sched_getaffinity(0))
    cache = directory / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = directory / NAME
    path.write_text(
        TEXT.format(
            cpus=cpus,
            worker_memory=int(STAND_IN_GIB * 2**30) // cpus,
            cache=cache,
            cache_bytes=int(STAND_IN_GIB * 2**30),
            feed=directory / "feed.jsonl",
        )
    )
    return path


def installed() -> bool:
    """Whether `ABU_RUNTIME` already names a stand-in this suite made (a python a test starts inherits it)."""
    named = os.environ.get("ABU_RUNTIME")
    return bool(named) and Path(named).name == NAME and Path(named).is_file()


def install() -> Path:
    """Point `ABU_RUNTIME` at a stand-in unless this process already inherited one; the folder goes at exit."""
    if not installed():
        directory = tempfile.mkdtemp(prefix=PREFIX)
        atexit.register(shutil.rmtree, directory, ignore_errors=True)
        os.environ["ABU_RUNTIME"] = str(write(Path(directory)))
    return Path(os.environ["ABU_RUNTIME"])
