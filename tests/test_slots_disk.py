"""The caller declares the disk floor; the shared API refuses before workload entry."""

import unittest
from unittest import mock

from abucache.slots import Admission, AdmissionError

from abu_decomp.admission import slots


class DiskFloorTest(unittest.TestCase):
    def test_policy_is_owned_by_the_shared_api(self):
        self.assertIsInstance(slots, Admission)
        self.assertEqual(slots.policy.disk_gib, 40)

    def test_disk_need_scales_with_work(self):
        with mock.patch.object(slots, "disk_free_gib", return_value=70):
            slots.check_disk("batch", 1)
            with self.assertRaises(AdmissionError):
                slots.check_disk("batch", 2)

    def test_no_work_starts_on_refusal(self):
        work = mock.Mock()
        with (
            mock.patch.object(slots, "disk_free_gib", return_value=1),
            mock.patch.object(slots, "pool_dir") as pool,
            self.assertRaises(AdmissionError),
            slots.hold("batch", label="stamp build"),
        ):
            work()
        work.assert_not_called()
        pool.assert_called()  # the refusal and the admission each read the pool; how often is the library's

    def test_owner_has_the_caller_declared_exemption(self):
        with mock.patch.object(slots, "disk_free_gib", return_value=1):
            slots.check_disk("owner", 8)
