"""Decomp resource policy; all admission behavior belongs to abucache.slots."""

from abucache.slots import Admission, Policy

slots = Admission(Policy(reserve_cores=2, floor_gib=0, disk_gib=40, rss_gib=0.75, stamp_gib=0, stamp_jobs=0))
