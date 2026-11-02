"""Profile values reach shared admission and stamp work only starts after a grant."""

import unittest
from contextlib import contextmanager
from unittest import mock

from abucache.slots import Admission, Policy

from abu_decomp import cli
from abu_decomp.admission import slots
from abu_decomp.data.trial import Builder


class StampMemoryTest(unittest.TestCase):
    def test_profile_configuration_is_shared_and_explicit(self):
        with mock.patch.object(slots, "policy", Policy(2, 0, 40, 0.75, 0, 0)):
            slots.configure({"floor_gib": 3, "stamp_gib": 6.5, "stamp_jobs": 4, "reserve_cores": 1})
            self.assertEqual(slots.policy, Policy(1, 3, 40, 0.75, 6.5, 4))
            slots.configure(None)
            self.assertEqual(slots.policy.stamp_gib, 6.5)
        self.assertIs(cli.slots, slots)
        self.assertIsInstance(cli.slots, Admission)

    def test_stamp_policy_and_work_order(self):
        events = []

        @contextmanager
        def hold(klass, **kwargs):
            self.assertEqual((klass, kwargs["weight"], kwargs["mem_gib"]), ("batch", 2, 5))
            events.append("grant")
            try:
                yield mock.Mock(weight=2, waited=0)
            finally:
                events.append("release")

        builder = object.__new__(Builder)
        with (
            mock.patch.object(slots, "policy", Policy(2, 4, 40, 0.75, 5, 2)),
            mock.patch.object(slots, "stamp_weight", return_value=2),
            mock.patch.object(slots, "hold", side_effect=hold),
            mock.patch.object(Builder, "held", False),
            builder.holding(),
        ):
            events.append("work")
        self.assertEqual(events, ["grant", "work", "release"])
