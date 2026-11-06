"""The unit suite never reads the host's own free disk, memory or runtime file: a host with less free disk than a job
needs, or with no ~/.config/abu/runtime.toml, still passes the unrelated suites, in this process and in every python
a test starts."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import standin_runtime  # found through host_seam, which conftest.py and ci/run_tests.py put on sys.path
from abucache import runtime
from abucache import slots as abucache_slots
from abucache.slots import AdmissionError

from abu_decomp.admission import slots

PROBE = (
    "from abu_decomp.admission import slots; slots.configure({'disk_gib': 40}); "
    "slots.acquire('batch', 1, 'probe').release()"
)
SEES = (
    "import os; from abucache import runtime; from abu_decomp.admission import slots; "
    "print(os.environ['ABU_RUNTIME']); print(runtime.load().cache_dir); print(slots.pool_dir())"
)


class HostSeam(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="slots-seam-")
        self.runtime_file = standin_runtime.write(Path(self.dir))
        patcher = mock.patch.dict(os.environ, {"ABU_RUNTIME": str(self.runtime_file), "UNBAKE_CORES": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        pool = mock.patch.object(abucache_slots, "default", abucache_slots.Slots(runtime.load(self.runtime_file)))
        pool.start()
        self.addCleanup(pool.stop)

    def starved_host(self):
        """The machine's statvfs reports 1 GiB free: the seam must keep every reader from ever asking it."""
        usage = os.statvfs_result((4096, 4096, 262144, 262144, 262144, 0, 0, 0, 0, 255))
        return mock.patch.object(os, "statvfs", return_value=usage)

    def test_the_free_space_reads_are_stand_ins_not_the_host(self) -> None:
        with self.starved_host() as statvfs:
            self.assertGreater(slots.disk_free_gib(), 40)
            self.assertGreater(slots._mem_gib("MemAvailable"), 40)
        statvfs.assert_not_called()

    def test_a_job_needing_more_than_the_host_has_free_still_runs_here(self) -> None:
        with self.starved_host(), mock.patch.object(slots.policy, "disk_gib", 40.0):
            slots.acquire("batch", 1, "seam").release()

    def test_a_python_the_test_starts_gets_the_same_stand_in(self) -> None:
        self.assertIn("host_seam", os.environ["PYTHONPATH"])
        done = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_python_the_test_starts_sees_the_stand_in_runtime_and_pools_under_its_cache(self) -> None:
        done = subprocess.run([sys.executable, "-c", SEES], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        named, cache, pool = (Path(line) for line in done.stdout.splitlines())
        self.assertEqual(named, self.runtime_file)
        self.assertEqual(cache, self.runtime_file.parent / "cache")
        self.assertTrue(pool.is_relative_to(cache), f"{pool} is not under {cache}")
        self.assertEqual(pool, slots.pool_dir())
        self.assertFalse(pool.is_relative_to(Path.home() / ".config"))

    def test_the_stand_in_runtime_file_is_complete_and_derives_its_memory_from_the_seam(self) -> None:
        loaded = runtime.load(self.runtime_file)
        self.assertEqual(loaded.workers, len(os.sched_getaffinity(0)))
        stand_in = int(standin_runtime.STAND_IN_GIB * 2**30)
        self.assertEqual(loaded.worker_memory, stand_in // loaded.cores)

    def test_the_floor_itself_is_still_checked_when_a_test_sets_the_free_space(self) -> None:
        with (
            mock.patch.object(slots.policy, "disk_gib", 40.0),
            mock.patch.object(slots, "disk_free_gib", return_value=1.0),
            self.assertRaises(AdmissionError),
        ):
            slots.acquire("batch", 1, "starved")
