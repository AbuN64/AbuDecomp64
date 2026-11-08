import importlib.util
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("ensure_venv", Path(__file__).parent.parent / "ci" / "ensure_venv.py")
ensure_venv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ensure_venv)
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from abu_decomp import sources  # noqa: E402

REPO = Path(__file__).parent.parent
UNBAKE = "https://github.com/AbuN64/cake-unbake-64"
PYPROJECT = """[project]
name = "x"
requires-python = ">=3.13"
dependencies = ["unbake", "gcovr==8.3"]
[project.optional-dependencies]
dev = ["ruff==0.16.9", "pytest==9.1.1"]
"""


class SiblingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.workspace = Path(self.dir.name)
        self.root = self.workspace / "consumer"
        self.root.mkdir()
        (self.root / "pyproject.toml").write_text(PYPROJECT)

    def checkout(self, name):
        path = self.workspace / name
        path.mkdir()
        (path / ".git").write_text("gitdir: worktree")
        (path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
        return path

    def test_every_tool_uses_sibling_when_present_and_landed_when_absent(self):
        lookup = mock.Mock(return_value=(0, f"{'a' * 40}\tHEAD\n"))
        for name, url in sources.PACKAGES.items():
            with self.subTest(package=name):
                self.assertEqual(
                    sources.clean_requirements([name], lookup, root=self.root), [f"{name} @ git+{url}@{'a' * 40}"]
                )
                path = self.checkout(name)
                lookup.reset_mock()
                self.assertEqual(sources.clean_requirements([name], lookup, root=self.root), [str(path)])
                lookup.assert_not_called()

    def test_duplicate_checkouts_refuse(self):
        self.checkout("abucache")
        other = self.workspace / "duplicate"
        other.mkdir()
        (other / ".git").mkdir()
        (other / "pyproject.toml").write_text('[project]\nname = "abucache"\n')
        with self.assertRaisesRegex(sources.SourceError, "multiple sibling checkouts"):
            sources.sibling_checkouts(self.root)

    def test_stamp_tracks_commit_dirty_content_untracked_content_and_deletion(self):
        path = self.checkout("abucache")
        head, diff, untracked = "a" * 40, "", ""

        def git(argv, cwd):
            self.assertEqual(cwd, path)
            return 0, {"rev-parse": head, "diff": diff, "ls-files": untracked}[argv[1]]

        def key():
            return ensure_venv.key(self.root, [str(path)], git)

        before = key()
        self.assertEqual(before, key())
        head = "b" * 40
        committed = key()
        self.assertNotEqual(before, committed)
        diff = "first dirty content"
        dirty = key()
        self.assertNotEqual(committed, dirty)
        diff = "second dirty content"
        self.assertNotEqual(dirty, key())
        untracked = "new.py\0"
        (path / "new.py").write_text("first")
        first = key()
        (path / "new.py").write_text("second")
        self.assertNotEqual(first, key())
        (path / "new.py").unlink()
        untracked = ""
        self.assertNotEqual(first, key())

    def test_checkout_git_failure_refuses(self):
        path = self.checkout("abucache")
        with self.assertRaisesRegex(sources.SourceError, "git rev-parse failed"):
            sources.checkout_key(path, mock.Mock(return_value=(1, "broken checkout")))


class EnsureTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        (self.root / "pyproject.toml").write_text(PYPROJECT)
        (self.root / "src").symlink_to(REPO / "src")
        self.heads = {url: "a" * 40 for url in sources.PACKAGES.values()}
        self.calls = []

    def ls_remote(self, argv, cwd):
        """Stands in for `git ls-remote URL HEAD`: each tool's source answers the commit it has landed at."""
        return 0, f"{self.heads[argv[2]]}\tHEAD\n"

    def run_stub(self, argv, **kwargs):
        """Stands in for subprocess.run: `venv` makes the interpreter file, `pip` records the install."""
        self.calls.append(argv)
        if argv[1:3] == ["-m", "venv"]:
            (self.root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
            (self.root / ".venv" / "bin" / "python").write_text("")

    def ensure(self, version=(3, 13)):
        return ensure_venv.ensure(self.root, run=self.run_stub, version=version, lookup=self.ls_remote)

    def installs(self):
        return [call for call in self.calls if call[1:3] == ["-m", "pip"]]

    def test_first_run_makes_the_environment_and_installs_every_declared_requirement(self):
        python = self.ensure()
        self.assertEqual(python, self.root / ".venv" / "bin" / "python")
        self.assertEqual(
            self.installs()[0][-4:],
            [f"unbake @ git+{UNBAKE}@{'a' * 40}", "gcovr==8.3", "ruff==0.16.9", "pytest==9.1.1"],
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

    def test_a_tool_that_landed_a_new_commit_installs_again_and_one_that_did_not_does_not(self):
        self.ensure()
        self.calls.clear()
        self.heads["https://github.com/AbuN64/recipe-maker-64"] = "b" * 40  # not a dependency of this tree
        self.ensure()
        self.assertEqual(self.calls, [])
        self.heads[UNBAKE] = "c" * 40
        self.ensure()
        self.assertIn(f"unbake @ git+{UNBAKE}@{'c' * 40}", self.installs()[0])

    def test_new_abucache_revision_changes_the_environment_key(self):
        (self.root / "pyproject.toml").write_text(PYPROJECT.replace('["unbake",', '["abucache", "unbake",'))
        self.ensure()
        before = (self.root / ".venv" / ensure_venv.STAMP).read_text()
        self.calls.clear()
        self.heads[sources.PACKAGES["abucache"]] = "d" * 40
        self.ensure()
        self.assertNotEqual(before, (self.root / ".venv" / ensure_venv.STAMP).read_text())
        self.assertIn(f"abucache @ git+{sources.PACKAGES['abucache']}@{'d' * 40}", self.installs()[0])

    def test_fresh_python_bootstraps_without_abucache_or_the_runtime(self):
        script = r"""
import builtins, importlib.util, sys
from pathlib import Path
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith('abucache') or name in ('abu_decomp.deploy', 'abu_decomp.tools'):
        raise AssertionError('bootstrap imported runtime: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('bootstrap', root / 'ci/ensure_venv.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
rows = module.requirements(root, lambda argv, cwd: (0, 'a' * 40 + '\tHEAD\n'))
from abu_decomp import sources
checkout = sources.sibling_checkouts(root).get('abucache')
assert str(checkout) in rows if checkout else any(row.startswith('abucache @ git+') for row in rows)
assert 'abucache' not in sys.modules
assert 'abu_decomp.deploy' not in sys.modules
assert 'abu_decomp.tools' not in sys.modules
"""
        done = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(REPO)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_source_that_cannot_answer_fails_before_anything_is_made(self):
        def down(argv, cwd):
            return 128, "fatal: unable to access"

        with self.assertRaisesRegex(Exception, "ls-remote"):
            ensure_venv.ensure(self.root, run=self.run_stub, version=(3, 13), lookup=down)
        self.assertEqual(self.calls, [])

    def test_a_failed_install_leaves_no_stamp_so_the_next_run_installs_again(self):
        def failing(argv, **kwargs):
            self.run_stub(argv)
            if argv[1:3] == ["-m", "pip"]:
                raise RuntimeError("pip failed")

        with self.assertRaises(RuntimeError):
            ensure_venv.ensure(self.root, run=failing, version=(3, 13), lookup=self.ls_remote)
        self.calls.clear()
        self.ensure()
        self.assertEqual(len(self.installs()), 1)

    def test_an_older_python_is_refused_before_anything_is_made(self):
        with self.assertRaises(SystemExit):
            self.ensure(version=(3, 12))
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / ".venv").exists())


class RepoEnvironmentIgnore(unittest.TestCase):
    def test_symlinked_venv_leaves_git_status_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            target = Path(directory) / "environment"
            target.mkdir()
            (root / ".gitignore").write_bytes((REPO / ".gitignore").read_bytes())

            def git(*args):
                return subprocess.run(
                    ["git", "-c", "core.excludesFile=/dev/null", *args],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout

            git("init", "--quiet")
            git("add", ".gitignore")
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "Fixture",
            )
            venv = root / ".venv"
            venv.symlink_to(target, target_is_directory=True)
            self.assertEqual(git("status", "--porcelain"), "")
            target.rmdir()
            self.assertEqual(git("status", "--porcelain"), "", "dangling symlink")
            venv.unlink()
            venv.write_text("environment marker")
            self.assertEqual(git("status", "--porcelain"), "", "regular file")
            venv.unlink()
            venv.mkdir()
            (venv / "marker").touch()
            self.assertEqual(git("status", "--porcelain"), "", "directory")
            (root / "untracked.txt").touch()
            self.assertEqual(git("status", "--porcelain"), "?? untracked.txt\n")


class RepoDeclaresNoPins(unittest.TestCase):
    def test_every_own_tool_is_a_bare_name_and_nothing_pins_a_commit(self):
        text = (REPO / "pyproject.toml").read_text()
        self.assertNotIn("git+", text)
        deps = tomllib.loads(text)["project"]["dependencies"]
        for name in (n for n in sources.PACKAGES if n != "abu-decomp-64"):
            self.assertIn(name, deps)

    def test_no_environment_variable_picks_the_interpreter(self):
        root = Path(__file__).parent.parent
        names = ("ABU_" + "PYTHON", "ABU_" + "VENV")  # spelled in halves so the repo-wide grep for them stays empty
        for path in [root / "bin" / "test", root / "ci" / "ensure_venv.py", root / "README.md"]:
            text = path.read_text()
            self.assertFalse(any(name in text for name in names), path)


if __name__ == "__main__":
    unittest.main()
