import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("ensure_venv", Path(__file__).parent.parent / "ci" / "ensure_venv.py")
ensure_venv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ensure_venv)

PYPROJECT = """[project]
name = "x"
requires-python = ">=3.13"
dependencies = ["unbake @ git+https://example.lan/unbake@abc", "gcovr==8.3"]
[project.optional-dependencies]
dev = ["ruff==0.16.9", "pytest==9.1.1"]
"""


class EnsureTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        (self.root / "pyproject.toml").write_text(PYPROJECT)
        self.calls = []

    def run_stub(self, argv, **kwargs):
        """Stands in for subprocess.run: `venv` makes the interpreter file, `pip` records the install."""
        self.calls.append(argv)
        if argv[1:3] == ["-m", "venv"]:
            (self.root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
            (self.root / ".venv" / "bin" / "python").write_text("")

    def ensure(self, version=(3, 13)):
        return ensure_venv.ensure(self.root, run=self.run_stub, version=version)

    def installs(self):
        return [call for call in self.calls if call[1:3] == ["-m", "pip"]]

    def test_first_run_makes_the_environment_and_installs_every_declared_requirement(self):
        python = self.ensure()
        self.assertEqual(python, self.root / ".venv" / "bin" / "python")
        self.assertEqual(
            self.installs()[0][-4:],
            ["unbake @ git+https://example.lan/unbake@abc", "gcovr==8.3", "ruff==0.16.9", "pytest==9.1.1"],
        )

    def test_unchanged_pyproject_reuses_the_environment_with_no_install(self):
        self.ensure()
        self.calls.clear()
        self.ensure()
        self.assertEqual(self.calls, [])

    def test_changed_pyproject_installs_again(self):
        self.ensure()
        self.calls.clear()
        (self.root / "pyproject.toml").write_text(PYPROJECT.replace("gcovr==8.3", "gcovr==9.0"))
        self.ensure()
        self.assertEqual(len(self.installs()), 1)
        self.assertIn("gcovr==9.0", self.installs()[0])

    def test_a_failed_install_leaves_no_stamp_so_the_next_run_installs_again(self):
        def failing(argv, **kwargs):
            self.run_stub(argv)
            if argv[1:3] == ["-m", "pip"]:
                raise RuntimeError("pip failed")

        with self.assertRaises(RuntimeError):
            ensure_venv.ensure(self.root, run=failing, version=(3, 13))
        self.calls.clear()
        self.ensure()
        self.assertEqual(len(self.installs()), 1)

    def test_an_older_python_is_refused_before_anything_is_made(self):
        with self.assertRaises(SystemExit):
            self.ensure(version=(3, 12))
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / ".venv").exists())


class RepoDeclaresInstallablePins(unittest.TestCase):
    def test_every_own_tool_is_pinned_to_a_fetchable_commit(self):
        deps = ensure_venv.requirements(Path(__file__).parent.parent)
        for name in ("unbake", "recipemaker", "cake-taste-64"):
            (row,) = [d for d in deps if d.startswith(name + " @ git+https://")]
            self.assertRegex(row, r"@[0-9a-f]{40}$")

    def test_no_environment_variable_picks_the_interpreter(self):
        root = Path(__file__).parent.parent
        names = ("ABU_" + "PYTHON", "ABU_" + "VENV")  # spelled in halves so the repo-wide grep for them stays empty
        for path in [root / "bin" / "test", root / "ci" / "ensure_venv.py", root / "README.md"]:
            text = path.read_text()
            self.assertFalse(any(name in text for name in names), path)


if __name__ == "__main__":
    unittest.main()
