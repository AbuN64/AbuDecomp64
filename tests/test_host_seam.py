"""The unit suite never reads the host's own free disk or memory: a host with less free disk than a job needs still
passes the unrelated suites, in this process and in every python a test starts."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from abucache.slots import AdmissionError

from abu_decomp.admission import slots

PROBE = (
    "from abu_decomp.admission import slots; slots.configure({'disk_gib': 40}); "
    "slots.acquire('batch', 1, 'probe').release()"
)


class HostSeam(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="slots-seam-")
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.dir, "UNBAKE_CORES": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_the_floor_itself_is_still_checked_when_a_test_sets_the_free_space(self) -> None:
        with (
            mock.patch.object(slots.policy, "disk_gib", 40.0),
            mock.patch.object(slots, "disk_free_gib", return_value=1.0),
            self.assertRaises(AdmissionError),
        ):
            slots.acquire("batch", 1, "starved")
