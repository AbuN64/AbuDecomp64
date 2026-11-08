"""Every python the suite starts (the runner and conftest put this folder first on PYTHONPATH) reads a stand-in for the
host's free disk and memory, never the machine's own: a unit test's result must not depend on how full or loaded the
host is. The one seam is slots.disk_free_gib and slots._mem_gib; a test that checks a floor patches them itself.
Likewise each reads a stand-in runtime file (ABU_RUNTIME, standin_runtime.py), never the operator's own, and a
stand-in admitted width (JOBS, STAND_IN_JOBS), never the one the suite itself was admitted with: every width a command
picks is at most JOBS (abu_decomp.admission.width), so a result must not depend on the caller's admission either. A
test of the refusal passes its own environ."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import standin_runtime  # noqa: E402

STAND_IN_GIB = standin_runtime.STAND_IN_GIB
STAND_IN_JOBS = "8"
os.environ["JOBS"] = STAND_IN_JOBS
standin_runtime.install()

try:
    from abu_decomp.admission import slots
except ImportError:  # a python that does not import the checkout has no pool to protect
    pass
else:
    slots.disk_free_gib = lambda output_dir: STAND_IN_GIB
    slots._mem_gib = lambda field: STAND_IN_GIB
