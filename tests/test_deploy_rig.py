import atexit
import importlib.util
import io
import json
import os
import shutil
import site
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
from unittest import mock

spec = importlib.util.spec_from_file_location("deploy_rig", Path(__file__).parent.parent / "ci" / "deploy_rig.py")
deploy_rig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_rig)

SHA = "a" * 12 + "b" * 28
HOME = tempfile.mkdtemp(prefix="deploy-rig-home-")  # the name step opens builds in its home: a mocked one
atexit.register(shutil.rmtree, HOME, ignore_errors=True)
TOOLKIT = f"{HOME}/venv-decomp"
NAME = "0123456789ab"  # the engine's digest of the whole content; never the SHA
ENVIRON = {deploy_rig.ENV: SHA, "TOOLKIT": TOOLKIT, "HOME": HOME}
ENVELOPE = json.dumps({"result": {"name": NAME}}) + "\n"


def suite_facts(out):
    facts = {}
    for line in out.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key.startswith("suite-"):
            facts[key] = value
    return facts


class Bash:
    """The seam: records each script run with its arguments and answers (exit code, stdout) from a queue."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, script, *args, capture=False):
        self.calls.append((script, args, capture))
        return self.answers.pop(0)


def main(step, bash, environ=ENVIRON):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = deploy_rig.main([step, *ARGS], environ, bash)
    return code, out.getvalue(), err.getvalue()


ARGS = ["--Subject.Board", "board1", "--Subject.Card", "card1"]  # the close appends them; no step reads them


class StepTests(unittest.TestCase):
    def setUp(self):
        os.makedirs(HOME, exist_ok=True)

    def test_name_prints_the_engines_name(self):
        bash = Bash((0, "noise\n" + ENVELOPE))
        code, out, _ = main("name", bash)
        self.assertEqual(code, 0)
        self.assertIn(f"name: {NAME}", out)
        self.assertEqual(bash.calls, [(deploy_rig.NAME, (SHA, TOOLKIT, HOME), True)])

    def test_a_failed_name_refuses(self):
        code, _, err = main("name", Bash((1, "name-error\n")))
        self.assertEqual(code, 1)
        self.assertIn("name exit 1", err)

    def test_build_names_the_build_then_runs_build_and_suite_with_the_name(self):
        bash = Bash((0, ENVELOPE), (0, ""))
        code, _, _ = main("build", bash)
        self.assertEqual(code, 0)
        script, args, capture = bash.calls[1]
        self.assertEqual((args, capture), ((SHA, TOOLKIT, NAME, HOME), False))
        self.assertTrue(script.startswith(deploy_rig.BUILD))
        self.assertIn("pytest.main", script)
        self.assertNotIn("__RUNNER__", script)

    def test_a_failed_name_stops_build_before_any_build(self):
        bash = Bash((1, ""))
        code, _, err = main("build", bash)
        self.assertEqual(code, 1)
        self.assertEqual(len(bash.calls), 1)
        self.assertIn("name exit 1", err)

    def test_a_failing_build_or_suite_refuses_with_the_links_unchanged(self):
        code, _, err = main("build", Bash((0, ENVELOPE), (1, "")))
        self.assertEqual(code, 1)
        self.assertIn("build exit 1, links unchanged", err)

    def test_readback_reads_the_build_linked_for_the_landed_sha(self):
        bash = Bash((0, ""))
        self.assertEqual(main("readback", bash)[0], 0)
        self.assertEqual(bash.calls, [(deploy_rig.READBACK, (SHA, TOOLKIT, f"{TOOLKIT}.{SHA}"), False)])
        code, _, err = main("readback", Bash((1, "")))
        self.assertEqual((code, "readback exit 1" in err), (1, True))

    def test_serve_restarts_the_unit_for_the_landed_sha(self):
        bash = Bash((0, ""))
        self.assertEqual(main("serve", bash)[0], 0)
        self.assertEqual(bash.calls, [(deploy_rig.SERVE, (SHA, HOME), False)])
        code, _, err = main("serve", Bash((1, "")))
        self.assertEqual((code, "serve exit 1" in err), (1, True))

    def test_an_unknown_or_missing_step_refuses_before_any_script(self):
        for step in ("deploy", ""):
            bash = Bash()
            code, _, err = main(step, bash)
            self.assertEqual(code, 1)
            self.assertIn("the step is one of", err)
            self.assertEqual(bash.calls, [])

    def test_the_landed_sha_must_be_forty_hex_and_the_toolkit_absolute(self):
        for environ, want in (
            ({}, "is not a 40-hex SHA"),
            ({deploy_rig.ENV: "abc", "TOOLKIT": TOOLKIT}, "is not a 40-hex SHA"),
            ({deploy_rig.ENV: SHA}, "TOOLKIT is not an absolute path"),
            ({deploy_rig.ENV: SHA, "TOOLKIT": "venv"}, "TOOLKIT is not an absolute path"),
        ):
            bash = Bash()
            code, _, err = main("name", bash, environ)
            self.assertEqual(code, 1)
            self.assertIn(want, err)
            self.assertEqual(bash.calls, [])

    def test_no_step_calls_the_close_and_the_orchestrator_is_gone(self):
        text = (Path(__file__).parent.parent / "ci" / "deploy_rig.py").read_text()
        self.assertNotIn("mo-butlermaster", text.replace("`mo-butlermaster", ""))
        for gone in ("job_invoke", "answer_of", "newest_main", "have_commit", "Subject.Board", "def deploy("):
            self.assertFalse(hasattr(deploy_rig, gone) or f"def {gone}" in text, gone)

    def test_the_deploy_file_is_the_step_grammar(self):
        lines = (Path(__file__).parent.parent / ".deploy").read_text().splitlines()
        rig_toolkit = lines[0].removeprefix("deploy-env TOOLKIT=")  # the activation needs the live link absolute
        self.assertTrue(rig_toolkit.startswith("/") and rig_toolkit.endswith("/venv-decomp"))
        self.assertNotIn("/home", (Path(__file__).parent.parent / "ci" / "deploy_rig.py").read_text())
        self.assertEqual(
            [line.split()[:4] for line in lines[1:]],
            [
                ["rig", "run", "ci/deploy_rig.py", "name"],
                ["rig", "activate", "ci/deploy_rig.py", rig_toolkit],
                ["rig", "run", "ci/deploy_rig.py", "readback"],
                ["rig", "run", "ci/deploy_rig.py", "profile"],
                ["rig", "run", "ci/deploy_rig.py", "serve"],
            ],
        )
        self.assertEqual(lines[2].split()[4:], ["build"])
        self.assertTrue(os.access(Path(__file__).parent.parent / "ci" / "deploy_rig.py", os.X_OK))


class ReadbackTests(unittest.TestCase):
    """The readback step publishes the command and the login block; the deploy itself never prunes."""

    def test_readback_publishes_decomp_by_name_and_the_login_block_names_only_the_command_dir(self):
        home = Path(HOME)
        build = home / "venv-decomp.abc"
        wrapper(build / "bin" / "decomp", "exit 0")
        wrapper(build / "bin" / "unbake", "exit 0")
        Path(TOOLKIT).unlink(missing_ok=True)
        Path(TOOLKIT).symlink_to(build)
        code, _, _ = main("readback", Bash((0, "")))
        folder = home / deploy_rig.COMMANDS
        self.assertEqual((code, sorted(os.listdir(folder))), (0, ["decomp"]))  # unbake is never published from here
        self.assertEqual((folder / "decomp").resolve(), (build / "bin" / "decomp").resolve())
        (home / ".profile").write_text(
            "a\n# rig-deploy decomp defaults\nexport PATH=old:$PATH\n# end rig-deploy decomp defaults\nz\n"
        )
        deploy_rig.install_login_defaults(home, str(folder), home / "p.json")
        deploy_rig.install_login_defaults(home, str(folder), home / "p.json")  # idempotent
        profile = (home / ".profile").read_text()
        self.assertEqual(profile.count(deploy_rig.OPEN), 1)
        # guarded: the command dir lands on PATH once
        self.assertIn(f'*) export PATH={folder}:"$PATH" ;; esac', profile)
        self.assertNotIn("old", profile)
        self.assertTrue(profile.startswith("a\n") and profile.endswith("z\n"))
        self.assertIn(deploy_rig.OPEN, (home / ".zshenv").read_text())  # a file with no block gets one appended

    def test_the_deploy_has_no_prune(self):
        self.assertFalse(hasattr(deploy_rig, "prune"))
        self.assertNotIn("rmtree", Path(deploy_rig.__file__).read_text())


def wrapper(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def sh_env(**extra):
    return {"PATH": os.environ["PATH"], "PYTHONUSERBASE": site.getuserbase(), **extra}


class ScriptCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def bash(self, script, *args, cwd=None, env=None):
        return subprocess.run(
            ["bash", "-c", script, "job", *args],
            cwd=cwd,
            env=env or sh_env(),
            capture_output=True,
            text=True,
            check=False,
        )


def git(*args, cwd):
    identity = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True)


class SuiteScriptTests(ScriptCase):
    """The SUITE script and RUNNER, run for real in a tiny worktree with a stand-in BUILD venv."""

    def setUp(self):
        super().setUp()
        self.repo, self.built = self.root / "outer" / "repo", self.root / "built"
        for path in (self.repo / "src/abu_decomp", self.repo / "tests"):
            path.mkdir(parents=True)
        (self.root / "outer/pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        (self.repo / "pyproject.toml").write_text('[project]\nname = "tiny"\n')
        (self.repo / "src/abu_decomp/__init__.py").write_text("VALUE = 1\n")
        self.write_test("assert abu_decomp.VALUE == 1")
        wrapper(self.built / "bin/python", f'exec "{sys.executable}" "$@"')
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "c", cwd=self.repo)

    def write_test(self, body):
        (self.repo / "tests/test_it.py").write_text(
            f"import abu_decomp\n\n\ndef test_it():\n    {body}\n\n\ndef test_other():\n    pass\n"
        )

    def suite(self, sha=None):
        script = deploy_rig.SUITE.replace("__RUNNER__", deploy_rig.RUNNER)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True
        ).stdout.strip()
        done = self.bash(
            script,
            head if sha is None else sha,
            cwd=self.repo,
            env=sh_env(BUILD=str(self.built), built=str(self.built)),
        )
        return done, suite_facts(done.stdout)

    def test_a_passing_suite_reports_the_import_from_the_worktree_and_writes_suite_txt(self):
        done, facts = self.suite()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        self.assertEqual(facts["suite-sha"], sha)
        self.assertEqual(facts["suite-checkout"], str(self.repo.resolve()))
        self.assertTrue(facts["suite-summary"].startswith("2 passed, 0 failed, 0 skipped in "))
        self.assertTrue(PurePosixPath(facts["suite-import"]).is_relative_to(facts["suite-checkout"]))
        self.assertIn(f"suite-sha: {sha}", (self.built / "suite.txt").read_text())

    def test_the_suite_leaves_nothing_in_the_checkout(self):
        self.write_test("import os; assert not os.environ['TMPDIR'].startswith(os.getcwd())")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "t", cwd=self.repo)
        done, _ = self.suite()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(status.stdout, "")
        self.assertFalse((self.repo / ".tmp").exists())

    def test_a_passing_suite_leaves_the_built_tree_read_only_and_a_failing_one_leaves_it_alone(self):
        (self.built / "lib").mkdir()
        self.built.chmod(0o555)  # the deploy sealed it after its smoke test
        self.addCleanup(self.unseal)
        done, _ = self.suite()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for path in (self.built, self.built / "lib", self.built / "suite.txt", self.built / "bin/python"):
            self.assertFalse(path.stat().st_mode & 0o222, path)
        with self.assertRaises(PermissionError):
            (self.built / "pip-wrote-this").write_text("no")
        self.unseal()
        (self.built / "suite.txt").unlink()
        self.write_test("assert abu_decomp.VALUE == 2")
        done, _ = self.suite()
        self.assertEqual(done.returncode, 1)
        self.assertTrue(self.built.stat().st_mode & 0o200)  # the gate failed: no seal

    def unseal(self):
        subprocess.run(["chmod", "-R", "u+w", str(self.built)], check=False)

    def test_a_failing_test_exits_1_names_the_first_failure_and_writes_no_suite_txt(self):
        self.write_test("assert abu_decomp.VALUE == 2")
        done, facts = self.suite()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(facts["suite-first-failure"], "tests/test_it.py::test_it")
        self.assertFalse((self.built / "suite.txt").exists())

    def test_a_tree_at_another_commit_than_the_landed_one_exits_1_before_any_test(self):
        done, facts = self.suite(sha=SHA)
        self.assertEqual(done.returncode, 1)
        self.assertIn("not the landed", done.stderr)
        self.assertNotIn("suite-summary", facts)


class BuildScriptTests(ScriptCase):
    """The BUILD script with a stand-in toolkit python running `-m abu_decomp tools deploy`.

    The stand-in builds `<venv>.<name>` and swaps the link it is given: BUILD itself.
    """

    def setUp(self):
        super().setUp()
        self.toolkit = self.root / "venv-decomp"
        self.build = self.root / f"venv-decomp.{NAME}"

    def decomp(self, body):
        wrapper(self.toolkit / "bin/python", body)

    def run_build(self):
        env = sh_env(BUILD=str(self.build), HOME=str(self.root))  # the caller's own environment, as Dos admits it
        return self.bash(deploy_rig.BUILD, SHA, str(self.toolkit), NAME, str(self.root), env=env)

    def test_the_build_links_build_to_its_own_venv_and_never_touches_the_live_link(self):
        live = self.root / "live-target"
        wrapper(
            live / "bin/python",
            'test "$1 $2 $3 $4 $5 $6 $7 $8 $9" = '
            '"-m abu_decomp tools deploy --package abu-decomp-64 --sha ' + SHA + ' --venv" || exit 7\n'
            "case $PYTHONPATH in */src) ;; *) exit 8 ;; esac\n"  # the landed commit's own code runs the install
            f'shift 9; test "$2 $3" = "--carry-from {self.toolkit}" || exit 6\n'  # the carried set is the live link's
            f'test "$4 $5" = "--name {NAME}" || exit 5\n'  # the name job's answer is the build the engine must give
            f'dir="$1.{NAME}"; mkdir -p "$dir/bin" && ln -sfn "$dir" "$1"',
        )
        self.toolkit.symlink_to(live)
        done = self.run_build()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        built = self.root / f"venv-decomp.{NAME}.{NAME}"
        self.assertEqual(self.build.resolve(), built.resolve())
        self.assertEqual([p.name for p in self.root.iterdir() if "stage" in p.name], [])
        self.assertEqual(self.toolkit.resolve(), live.resolve())
        self.assertIn(f"deploy-build: {built}", done.stdout)

    def test_a_failing_engine_exits_1_and_links_nothing(self):
        live = self.root / "live-target"
        wrapper(live / "bin/python", "echo engine said no >&2; exit 3")
        self.toolkit.symlink_to(live)
        done = self.run_build()
        self.assertEqual(done.returncode, 1)
        self.assertIn("deploy-error: decomp tools deploy exit 3", done.stderr)
        self.assertEqual(self.build.resolve(), live.resolve())  # BUILD never left the live toolkit

    def test_a_build_link_that_reads_another_build_exits_1(self):
        live = self.root / "live-target"
        wrapper(live / "bin/python", f'mkdir -p "{self.root}/other" && shift 9 && ln -sfn "{self.root}/other" "$1"')
        self.toolkit.symlink_to(live)
        done = self.run_build()
        self.assertEqual(done.returncode, 1)
        self.assertIn("not the build", done.stderr)


class NameScriptTests(ScriptCase):
    """The NAME script asks the landed commit's own engine for the build's name and answers its envelope."""

    def setUp(self):
        super().setUp()
        self.toolkit = self.root / "venv-decomp"

    def test_the_name_script_runs_the_worktree_engine_over_the_live_link_and_a_failure_exits_1(self):
        live = self.root / "live-target"
        wrapper(
            live / "bin/python",
            'test "$*" = "-m abu_decomp tools name --package abu-decomp-64 --sha '
            + SHA
            + f' --carry-from {self.toolkit}" || exit 7\n'
            "case $PYTHONPATH in */src) ;; *) exit 8 ;; esac\n"
            f'echo \'{{"ok": true, "result": {{"name": "{NAME}"}}}}\'',
        )
        self.toolkit.symlink_to(live)
        env = sh_env(HOME=str(self.root))
        done = self.bash(deploy_rig.NAME, SHA, str(self.toolkit), str(self.root), env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(deploy_rig.build_name(done.stdout), NAME)
        wrapper(live / "bin/python", "exit 3")
        failed = self.bash(deploy_rig.NAME, SHA, str(self.toolkit), str(self.root), env=env)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("name-error", failed.stderr)


class BuildNameTests(unittest.TestCase):
    def test_the_name_is_the_last_envelope_line_with_a_12_hex_name_and_anything_else_is_refused(self):
        text = 'noise\n{"result": {"name": "abcdef012345"}}\n'
        self.assertEqual(deploy_rig.build_name(text), "abcdef012345")
        for bad in ("", "no json", '{"result": {}}', '{"result": {"name": "../x"}}'):
            with self.assertRaisesRegex(deploy_rig.Refused, "no build name"):
                deploy_rig.build_name(bad)


class ReadbackScriptTests(ScriptCase):
    """The READBACK script against a stand-in venv: a link check, then the installed commit of each pin."""

    PIN = "c" * 40

    def setUp(self):
        super().setUp()
        self.repo = self.root / "worktree"
        self.repo.mkdir()
        (self.repo / "pyproject.toml").write_text(
            '[project]\nname = "tiny-decomp"\n'
            f'dependencies = ["textual==8.2.8", "tool-one @ git+https://example.lan/tool-one@{self.PIN}"]\n'
        )
        self.site = self.root / "site"
        self.venv = self.root / "venv.build"
        wrapper(self.venv / "bin/python", f'PYTHONPATH="{self.site}" exec "{sys.executable}" "$@"')
        self.toolkit = self.root / "venv-live"
        self.toolkit.symlink_to(self.venv)
        self.dist("tiny-decomp", SHA)
        self.dist("tool-one", self.PIN)
        self.seal()
        self.addCleanup(subprocess.run, ["chmod", "-R", "u+w", str(self.venv)], check=False)

    def seal(self):
        subprocess.run(["chmod", "-R", "a-w", str(self.venv)], check=True)

    def dist(self, name, commit):
        info = self.site / f"{name.replace('-', '_')}-1.0.dist-info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
        direct = {"url": "https://example.lan/x", "vcs_info": {"vcs": "git", "commit_id": commit}}
        (info / "direct_url.json").write_text(json.dumps(direct))

    def readback(self, build=None):
        return self.bash(deploy_rig.READBACK, SHA, str(self.toolkit), str(build or self.venv), cwd=self.repo)

    def test_matching_pins_and_link_exit_0_and_print_each_installed_commit(self):
        done = self.readback()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"readback-pin: tiny-decomp {SHA}", done.stdout)
        self.assertIn(f"readback-pin: tool-one {self.PIN}", done.stdout)

    def test_a_read_only_live_tree_prints_the_refused_write_and_a_writable_one_exits_1(self):
        done = self.readback()
        self.assertIn("readback-readonly: a write into the live toolkit was refused", done.stdout)
        self.assertFalse((self.venv / ".readonly-probe").exists())
        subprocess.run(["chmod", "-R", "u+w", str(self.venv)], check=True)
        done = self.readback()
        self.assertEqual(done.returncode, 1)
        self.assertIn("must be read-only", done.stderr)
        self.assertFalse((self.venv / ".readonly-probe").exists())

    def test_a_pin_installed_at_another_commit_or_missing_exits_1_naming_it(self):
        self.dist("tool-one", "9" * 40)
        done = self.readback()
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"tool-one is {'9' * 40}, the pin is {self.PIN}", done.stderr)
        for path in (self.site / "tiny_decomp-1.0.dist-info").iterdir():
            path.unlink()
        (self.site / "tiny_decomp-1.0.dist-info").rmdir()
        done = self.readback()
        self.assertIn("tiny-decomp is not installed", done.stderr)

    def test_a_live_link_that_does_not_resolve_to_the_build_exits_1(self):
        other = self.root / "other"
        other.mkdir()
        done = self.readback(build=other)
        self.assertEqual(done.returncode, 1)
        self.assertIn("reads", done.stderr)
        self.assertNotIn("readback-pin", done.stdout)


if __name__ == "__main__":
    unittest.main()


class ServeScriptTests(ScriptCase):
    """The SERVE script for real in a tiny worktree, with systemctl and curl stand-ins that log their calls."""

    def run_serve(self, page, restart_exit=0):
        home, bins, calls = self.root / "home", self.root / "bin", self.root / "calls"
        (self.root / "work/ci").mkdir(parents=True)
        (self.root / "work/ci/abu-decomp-tas.service").write_text("[Service]\n")
        restart = f'[ "$2" = restart ] && exit {restart_exit}'
        wrapper(bins / "systemctl", f'echo "systemctl $*" >> {calls}\n{restart}\nexit 0')
        wrapper(bins / "curl", f"echo curl >> {calls}\nprintf '%s' '{page}'")
        env = sh_env(HOME=str(home), PATH=f"{bins}:{os.environ['PATH']}")
        done = self.bash(deploy_rig.SERVE, SHA, str(self.root / "home"), cwd=self.root / "work", env=env)
        return done, calls.read_text().splitlines() if calls.exists() else []

    def test_it_installs_the_unit_restarts_it_then_prints_the_served_sha(self):
        done, calls = self.run_serve(f'<meta name="deployed-sha" content="{SHA}">')
        self.assertEqual((done.returncode, done.stdout), (0, f"serve-sha: {SHA}\n"))
        self.assertEqual(
            calls,
            [
                "systemctl --user daemon-reload",
                "systemctl --user enable abu-decomp-tas.service",
                "systemctl --user restart abu-decomp-tas.service",
                "curl",
            ],
        )
        self.assertTrue((self.root / "home/.config/systemd/user/abu-decomp-tas.service").is_file())

    def test_a_failed_restart_exits_1_before_any_readback(self):
        done, calls = self.run_serve("", restart_exit=1)
        self.assertEqual(done.returncode, 1)
        self.assertIn("restart of abu-decomp-tas.service failed", done.stderr)
        self.assertNotIn("curl", calls)

    def test_a_page_without_the_meta_tag_exits_1(self):
        done, _ = self.run_serve("<html>old page</html>")
        self.assertEqual(done.returncode, 1)
        self.assertIn("no deployed-sha", done.stderr)

    def test_a_page_with_another_sha_exits_1(self):
        done, _ = self.run_serve('<meta name="deployed-sha" content="x">')
        self.assertEqual(done.returncode, 1)
        self.assertIn("serves x", done.stderr)


TEMPLATE_FILE = Path(__file__).parent.parent / "ci" / "tas-profile.json"


class ProfileTests(unittest.TestCase):
    """The deploy writes the live tas profile from the landed tree's template; nothing else edits it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        (self.home / ".config/abu-decomp").mkdir(parents=True)
        (self.home / "rig-state/tas").mkdir(parents=True)
        self.template = Path(tmp.name) / "tas-profile.json"
        self.live = self.home / "rig-state/tas/profile.json"

    def site(self, **values):
        (self.home / ".config/abu-decomp/site.json").write_text(json.dumps(values))

    def install(self, template):
        self.template.write_text(json.dumps(template))
        return deploy_rig.install_profile(self.template, self.home)

    def test_a_profile_key_unknown_to_the_tree_cannot_reach_the_live_profile(self):
        # the live file was rewritten by a run before its code landed: it holds a key this tree's template lacks
        self.live.write_text(json.dumps({"name": "tas", "tas": {"serve": {"kan": "x", "future_key": 1}}}))
        self.site(w="192.0.2.7")
        self.install({"name": "tas", "tas": {"serve": {"kan": "http://${w}:1", "root": "${home}/tas"}}})
        live = json.loads(self.live.read_text())
        want = {"name": "tas", "tas": {"serve": {"kan": "http://192.0.2.7:1", "root": f"{self.home}/tas"}}}
        self.assertEqual(live, want)
        self.assertNotIn("future_key", self.live.read_text())

    def test_a_site_key_the_template_does_not_use_refuses_and_leaves_the_live_file(self):
        self.live.write_text("old")
        self.site(w="192.0.2.7", future_key="1")
        with self.assertRaisesRegex(deploy_rig.Refused, "does not use: future_key"):
            self.install({"a": "${w}"})
        self.assertEqual(self.live.read_text(), "old")

    def test_a_name_without_a_site_value_refuses_and_leaves_the_live_file(self):
        self.live.write_text("old")
        self.site()
        with self.assertRaisesRegex(deploy_rig.Refused, "no value for: w"):
            self.install({"a": "${w}"})
        self.assertEqual(self.live.read_text(), "old")

    def test_a_site_file_that_sets_home_refuses(self):
        self.site(home="/elsewhere")
        with self.assertRaisesRegex(deploy_rig.Refused, "sets home"):
            self.install({"a": "${home}"})

    def test_a_missing_site_file_refuses(self):
        with self.assertRaisesRegex(deploy_rig.Refused, "profile input"):
            self.install({"a": "${home}"})

    def test_a_template_that_is_not_an_object_refuses(self):
        self.site()
        with self.assertRaisesRegex(deploy_rig.Refused, "not an object"):
            self.install(["${home}"])

    def test_the_profile_step_installs_from_the_home_in_the_environment(self):
        self.site(w="192.0.2.7")
        self.template.write_text(json.dumps({"a": "${w}"}))
        with mock.patch.object(deploy_rig, "TEMPLATE", self.template):
            code, out, _ = main("profile", Bash(), {**ENVIRON, "HOME": str(self.home)})
        self.assertEqual((code, json.loads(self.live.read_text())), (0, {"a": "192.0.2.7"}))
        self.assertIn(f"profile: {self.live}", out)

    def test_the_profile_step_refuses_a_relative_home(self):
        code, _, err = main("profile", Bash(), {**ENVIRON, "HOME": "home"})
        self.assertEqual(code, 1)
        self.assertIn("HOME is not an absolute path", err)


class TemplateTests(unittest.TestCase):
    """The tracked template is valid for the tree that carries it."""

    def test_the_template_renders_and_this_tree_accepts_every_serve_key(self):
        from abu_decomp import config, site_enhanced, tas, tas_serve

        text = deploy_rig.render_profile(TEMPLATE_FILE.read_text(), "/home/example", {"w": "192.0.2.7"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(text)
            profile = config.load_profile(path)
        serve = profile.section("tas")["serve"]
        self.assertEqual(serve.keys() - tas_serve._SERVE_KEYS, set())
        self.assertIsNotNone(site_enhanced.settings(serve["enhanced"]))
        tas.settings(profile)
        self.assertNotIn("${", text)


    def test_the_template_names_the_reviewed_ledger_beside_the_seat_executable_so_a_play_passes_tas_ledger(self):
        from abu_decomp import config, tas

        text = deploy_rig.render_profile(TEMPLATE_FILE.read_text(), "/home/example", {"w": "192.0.2.7"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(text)
            cfg = tas.settings(config.load_profile(path))
        self.assertEqual(cfg["ledger"], Path(cfg["port"]).parent / "lockstep-ignore.tsv")

    def test_the_template_names_no_site_fact(self):
        text = TEMPLATE_FILE.read_text()
        self.assertNotRegex(text, r"\d+\.\d+\.\d+\.\d+:\d+/")
        self.assertNotIn("/home/", text)
