"""Decomp resource policy; all admission behavior belongs to abucache.slots. Every width a decomp command picks for
itself (a pool, a replay fan, a make) is at most the JOBS Terminal admission gave it (`width`), never the host's CPUs
or a fixed number: a whole-host width over-subscribes the host the admission measured. No JOBS refuses."""

import os
from collections.abc import Mapping

from abucache.slots import Admission, AdmissionError, Policy

slots = Admission(Policy(reserve_cores=2, floor_gib=0, disk_gib=40, rss_gib=0.75, stamp_gib=0, stamp_jobs=0))


def admitted_jobs(environ: Mapping[str, str] | None = None) -> int:
    """The admitted `JOBS` of `environ` (os.environ by default); absent, empty or not a positive integer refuses."""
    value = (os.environ if environ is None else environ).get("JOBS", "")
    if not value.isdigit() or int(value) < 1:
        raise AdmissionError(
            "JOBS", f"{value!r} is not an admitted width", "run under Terminal admission (mo-terminal Admission Invoke)"
        )
    return int(value)


def width(given: int | None = None, environ: Mapping[str, str] | None = None) -> int:
    """A command's worker count: a positive `given` (a --jobs, a default) at most the admitted JOBS, else JOBS."""
    jobs = admitted_jobs(environ)
    return min(given, jobs) if given and given > 0 else jobs
