"""Every python the suite starts (the runner and conftest put this folder first on PYTHONPATH) reads a stand-in for the
host's free disk and memory, never the machine's own: a unit test's result must not depend on how full or loaded the
host is. The one seam is slots.disk_free_gib and slots._mem_gib; a test that checks a floor patches them itself."""

STAND_IN_GIB = 1024.0

try:
    from abu_decomp.admission import slots
except ImportError:  # a python that does not import the checkout has no pool to protect
    pass
else:
    slots.disk_free_gib = lambda: STAND_IN_GIB
    slots._mem_gib = lambda field: STAND_IN_GIB
