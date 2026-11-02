"""Real decomp entry points cross shared admission before their workload seams."""

import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from abucache.slots import AdmissionError, Policy

from abu_decomp import cli, proof, tas
from abu_decomp.admission import slots
from abu_decomp.behave import replay


class Consumers(unittest.TestCase):
    def test_cli_passes_profile_values_to_shared_policy(self):
        profile = mock.Mock()
        profile.section.return_value = {"slots": {"disk_gib": 55, "floor_gib": 3}}
        with (
            mock.patch.object(cli, "load_profile", return_value=profile),
            mock.patch.object(slots, "policy", Policy(2, 0, 40, 0.75, 0, 0)),
        ):
            self.assertIs(cli._profile(Path("seat.json")), profile)
            self.assertEqual((slots.policy.disk_gib, slots.policy.floor_gib), (55, 3))

    def test_tas_classes_and_work_order(self):
        for name, kwargs, klass, weight in (
            (None, {}, "owner", 1),
            ("movie", {"speed": 1}, "interactive", 1),
            ("movie", {"oracle": True}, "background", 2),
            ("movie", {"slot_class": "batch"}, "batch", 1),
        ):
            with self.subTest(klass=klass):
                events = []

                @contextmanager
                def hold(given, count, label, klass=klass, weight=weight, events=events):
                    self.assertEqual((given, count), (klass, weight))
                    events.append("grant")
                    yield
                    events.append("release")

                work = mock.Mock(side_effect=lambda *a, events=events, **k: events.append("work"))
                with (
                    mock.patch.object(slots, "hold", side_effect=hold),
                    mock.patch("abu_decomp.tas_bake.movie_hook"),
                ):
                    tas._admitted(work)(object(), name, **kwargs)
                self.assertEqual(events, ["grant", "work", "release"])

    def test_replay_refusal_never_starts_the_port(self):
        with (
            mock.patch.object(slots, "hold", side_effect=AdmissionError("slots.disk_floor", "full")) as hold,
            mock.patch.object(replay.subprocess, "Popen") as popen,
            self.assertRaises(AdmissionError),
        ):
            replay.run_port({}, Path("port"), Path("run"), "on", [])
        hold.assert_called_once_with("batch", 1, "run_port")
        popen.assert_not_called()

    def test_proof_passes_stamp_policy_before_starting_make(self):
        chosen = mock.Mock(project=Path("tree"), host=Path("host"))
        with (
            mock.patch.object(proof.tools, "settings", return_value=chosen),
            mock.patch.object(proof, "command", return_value=["make", "check"]),
            mock.patch.object(slots, "stamp_weight", return_value=2),
            mock.patch.object(slots, "policy", Policy(2, 4, 40, 0.75, 5, 2)),
            mock.patch.object(slots, "hold", side_effect=AdmissionError("slots.memory", "full")) as hold,
            mock.patch.object(proof.subprocess, "Popen") as popen,
            self.assertRaises(AdmissionError),
        ):
            proof.run(mock.Mock(), ["check"])
        hold.assert_called_once_with("batch", 2, "decomp make", mem_gib=5)
        popen.assert_not_called()
