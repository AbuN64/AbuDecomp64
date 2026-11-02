"""The captured weight-2 TAS regression uses the shared affinity boundary."""

import unittest
from unittest import mock

from abucache.slots import Grant

from abu_decomp.admission import slots


class BurnTest(unittest.TestCase):
    def test_weight_two_confines_both_n64host_children(self):
        # Captured demand: two n64host children each asked for 2.94 cores.
        demands = [2.94, 2.94]
        grant = mock.Mock(spec=Grant, numbers={0, 1})
        before = set(range(1, 13))
        with (
            mock.patch.object(slots, "acquire", return_value=grant),
            mock.patch("os.sched_getaffinity", return_value=before),
            mock.patch("os.sched_setaffinity") as pin,
            mock.patch.dict("os.environ", {"UNBAKE_CORES": ""}),
        ):
            with slots.hold("background", 2, "play tas"):
                assigned = pin.call_args.args[1]
                self.assertEqual(assigned, {1, 2})
                self.assertEqual(min(sum(demands), len(assigned)), 2)
            self.assertEqual(pin.call_args.args[1], before)
        grant.release.assert_called_once()
