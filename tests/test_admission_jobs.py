"""Every width a decomp command picks is at most the admitted JOBS, never the host's CPUs or a fixed number; no JOBS
refuses (abu_decomp.admission.width)."""

import os
import unittest
from unittest import mock

from abucache.slots import AdmissionError

from abu_decomp import admission, tas


class AdmittedWidth(unittest.TestCase):
    def test_a_width_is_the_admitted_jobs_or_less(self):
        self.assertEqual(admission.width(None, {"JOBS": "4"}), 4)
        self.assertEqual(admission.width(0, {"JOBS": "4"}), 4)
        self.assertEqual(admission.width(8, {"JOBS": "4"}), 4)  # a wider --jobs or default is cut to JOBS
        self.assertEqual(admission.width(2, {"JOBS": "4"}), 2)

    def test_no_admitted_jobs_refuses(self):
        for environ in ({}, {"JOBS": ""}, {"JOBS": "0"}, {"JOBS": "-1"}, {"JOBS": "auto"}):
            with self.subTest(environ=environ), self.assertRaises(AdmissionError) as refused:
                admission.width(2, environ)
            self.assertEqual(refused.exception.key, "JOBS")

    def test_play_all_never_fans_wider_than_jobs_whatever_the_slot_pool_holds(self):
        with (
            mock.patch.object(tas.slots, "capacity", return_value=64),
            mock.patch.dict(os.environ, {"JOBS": "3", "ABU_TAS_JOBS": "12"}),
        ):
            self.assertEqual(tas.all_jobs(), 3)
        with (
            mock.patch.object(tas.slots, "capacity", return_value=64),
            mock.patch.dict(os.environ, {"JOBS": ""}),
            self.assertRaises(AdmissionError),
        ):
            tas.all_jobs()

    def test_the_port_template_gate_takes_jobs(self):
        text = admission.__file__.rsplit("/", 1)[0] + "/port/template/ci/check"
        with open(text) as gate:
            body = gate.read()
        self.assertIn('-j"$JOBS"', body)
        self.assertIn("${JOBS:?", body)
        self.assertNotRegex(body, r"-j[0-9]")


if __name__ == "__main__":
    unittest.main()
