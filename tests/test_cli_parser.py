"""The root parser builds only the named command's arguments, so one command's stack never stops another parsing."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from abu_decomp import cli

SRC = Path(cli.__file__).resolve().parents[1]
WITHOUT_UNBAKE = (
    "import runpy, sys; sys.modules['unbake'] = None; sys.argv = ['decomp'] + sys.argv[1:]; "
    "runpy.run_module('abu_decomp', run_name='__main__')"
)


def test_display_input_help_parses_without_the_decomp_cycle_stack(tmp_path: Path) -> None:
    # Regression: data's argument builder imports unbake; display input --help exited 1 where unbake is absent.
    env = os.environ | {"PYTHONPATH": str(SRC)}
    argv = [sys.executable, "-c", WITHOUT_UNBAKE, "display", "--dir", str(tmp_path), "input", "--help"]
    done = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert "usage: decomp display input" in done.stdout


def test_selected_names_the_command_after_root_options() -> None:
    assert cli._selected(["--profile", "p.json", "display", "input", "--help"]) == "display"
    assert cli._selected(["--profile=p.json", "tas", "serve"]) == "tas"
    assert cli._selected(["--help"]) is None
    assert cli._selected(["nonsense", "display"]) is None


def test_parser_builds_only_the_selected_command() -> None:
    root = cli.parser(["display", "input", "--help"])
    group = next(a for a in root._actions if a.__class__.__name__ == "_SubParsersAction")
    assert any(a.dest == "action" for a in group.choices["display"]._actions)
    assert not any(a.dest == "action" for a in group.choices["tas"]._actions)


def test_parser_without_argv_builds_every_command() -> None:
    root = cli.parser()
    group = next(a for a in root._actions if a.__class__.__name__ == "_SubParsersAction")
    assert any(a.dest == "action" for a in group.choices["display"]._actions)
