import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import DecompError, cli, tas_coverage, tas_timelapse


class StageDurationTests(unittest.TestCase):
    def test_public_command_preserves_seconds_on_success_and_refusal(self):
        for failure in (None, DecompError("probe.failed", "broken")):
            events = []

            @contextlib.contextmanager
            def timed(name, events=events):
                events.append(("open", name))
                try:
                    yield SimpleNamespace(seconds=12.3456789)
                finally:
                    events.append(("closed", name))

            operation = mock.Mock()
            if failure is None:
                operation.run.return_value = ({"value": 7}, 0, [])
            else:
                operation.run.side_effect = failure
            output = io.StringIO()
            with (
                mock.patch.object(cli.cache_stages, "timed", timed),
                mock.patch.object(cli, "_tool_call", return_value=SimpleNamespace(command="probe", profile=None)),
                mock.patch.dict(cli.COMMANDS, {"probe": operation}),
                mock.patch.object(cli, "_record_claim"),
                contextlib.redirect_stdout(output),
            ):
                code = cli.main(["probe"])
            body = json.loads(output.getvalue())
            self.assertEqual(code, 0 if failure is None else 1)
            self.assertEqual(body["stages"], [{"name": "probe", "wall_seconds": 12.345679}])
            self.assertEqual([e[0] for e in events], ["open", "closed"])
            if failure:
                self.assertEqual(body["findings"][0]["key"], "probe.failed")

    def test_worker_stage_sums_observations_and_propagates_original_failure(self):
        stages = tas_coverage.Stages()
        events = []

        @contextlib.contextmanager
        def timed(name):
            try:
                yield SimpleNamespace(seconds=2.25)
            finally:
                events.append(name)

        failure = RuntimeError("worker failed")
        with mock.patch.object(tas_coverage.cache_stages, "timed", timed):
            with stages("compile"):
                pass
            with self.assertRaises(RuntimeError) as raised, stages("compile"):
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertEqual(stages.report(), {"compile": 4.5})
        self.assertEqual(events, ["compile", "compile"])

    def test_timelapse_reports_owner_seconds_and_preserves_ffmpeg_refusal(self):
        for failure in (False, True):
            events = []

            @contextlib.contextmanager
            def timed(name, events=events):
                try:
                    yield SimpleNamespace(seconds=3.456)
                except BaseException as exc:
                    events.append(type(exc))
                    raise
                finally:
                    events.append("closed")

            def encode(argv, failure=failure, **kwargs):
                Path(argv[-1]).write_bytes(b"video")
                return SimpleNamespace(returncode=1 if failure else 0, stderr="bad frame")

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                folder = root / "phase" / "frames" / "test"
                folder.mkdir(parents=True)
                (folder / "1.png").write_bytes(b"frame")
                with (
                    mock.patch.object(tas_timelapse.cache_stages, "timed", timed),
                    mock.patch.object(tas_timelapse.subprocess, "run", encode),
                ):
                    if failure:
                        with self.assertRaises(DecompError) as raised:
                            tas_timelapse.make(root, "test")
                        self.assertIn("bad frame", str(raised.exception))
                        self.assertEqual(events, [DecompError, "closed"])
                    else:
                        result = tas_timelapse.make(root, "test")
                        self.assertEqual(result["wall_seconds"], 3.46)
                        self.assertEqual(result["seconds"], 0.083)
                        self.assertEqual(events, ["closed"])
