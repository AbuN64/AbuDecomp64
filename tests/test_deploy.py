import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import deploy, tools
from abu_decomp.config import load_profile

U1, U2 = "1" * 40, "2" * 40
R1 = "3" * 40
D1 = "a" * 40
URL = deploy.PACKAGES
CARRIED = [
    "pycparser==3.0",
    f"recipemaker @ git+{URL['recipemaker']}@{R1}",
    f"unbake @ git+{URL['unbake']}@{U1}",
]


NAME1 = deploy.content_name([deploy.requirement("abu-decomp-64", D1)], CARRIED)  # the default deploy's build name


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "ci").mkdir(parents=True)
        (self.project / "versions" / "us-rev1").mkdir(parents=True)
        self.pin_file(unbake="0.5.12")
        for name in ("venv-a", "venv-b"):  # live paths: symlinks to a versioned venv
            self.make_venv(self.root / f"{name}.old")
            (self.root / name).symlink_to(self.root / f"{name}.old")
        (self.root / "host.toml").write_text("")
        sections = {"tools": {"project": str(self.project), "host": "host.toml", "timeout_seconds": 5}}
        (self.root / "seat.json").write_text(json.dumps({"schema_version": 1, "name": "seat", **sections}))
        self.profile = load_profile(self.root / "seat.json")
        self.commands = []
        self.fail_on = None
        self.carried = list(CARRIED)  # what the live venv holds
        self.requirements = ""  # requirements.txt of the project revision
        self.asked = []

    def requirements_of(self, project, remote, branch, sha):
        self.asked.append((project, remote, branch, sha))
        return sha or "c" * 40, self.requirements

    @staticmethod
    def make_venv(path):
        (path / "bin").mkdir(parents=True)
        (path / "bin" / "python").write_text("")

    def pin_file(self, **versions):
        rows = [
            f"pip install https://github.com/x/{name}/releases/download/v{v}/{name}-{v}-py3-none-any.whl"
            for name, v in versions.items()
        ]
        (self.project / "ci" / "run").write_text("\n".join(rows) + "\n")

    def runner(self, argv, cwd=None):
        self.commands.append(list(argv))
        # the live venv's python lists its packages
        if argv[1:2] == ["-c"] and deploy.CARRY in argv and not (self.fail_on and self.fail_on in deploy.CARRY):
            return 0, json.dumps(self.carried)
        if "venv" in argv and "-m" in argv and not (self.fail_on and self.fail_on in " ".join(argv)):
            self.make_venv(Path(argv[-1]))
        if self.fail_on and self.fail_on in " ".join(argv):
            return 1, "boom"
        return 0, "ok output"

    def deploy(self, sha=D1, venvs=("venv-a",), package="abu-decomp-64", extra=None):
        return deploy.deploy(
            self.profile,
            package,
            sha,
            [self.root / v for v in venvs],
            run=self.runner,
            requirements_of=self.requirements_of,
            **(extra or {}),
        )

    def test_one_package_is_installed_at_its_commit_beside_the_carried_ones_into_every_venv_and_smoke_tested(self):
        result, code, findings = self.deploy(venvs=("venv-a", "venv-b"))
        self.assertEqual((code, findings), (0, []))
        self.assertEqual(result["venvs"][0]["carried"], CARRIED)
        installs = [c for c in self.commands if "install" in c]
        self.assertEqual(len(installs), 4)  # per venv: the carried set, then the package
        carry, package = installs[0], installs[1]
        self.assertEqual(carry[carry.index("install") + 1 :], ["--no-deps", *CARRIED])  # as installed, not re-resolved
        self.assertEqual(
            package[package.index("install") + 1 :], [f"abu-decomp-64[dev] @ git+{deploy.DECOMP_URL}@{D1}"]
        )
        self.assertEqual(
            sum(c[0].endswith("decomp") and c[1] == "dependencies" for c in self.commands), 4
        )  # per venv: fresh, then via the link
        symbols = [c for c in self.commands if c[0].endswith("unbake")]
        self.assertEqual(len(symbols), 4)
        self.assertEqual(symbols[0][-3:], ["symbols", "--version", "us-rev1"])
        self.assertIn("--host", symbols[0])
        self.assertEqual(
            [row["venv"] for row in result["venvs"]], [str(self.root / "venv-a"), str(self.root / "venv-b")]
        )
        for name in ("venv-a", "venv-b"):  # swapped to a fresh versioned venv; the old tree is untouched
            self.assertEqual(os.path.realpath(self.root / name), str(self.root / f"{name}.{NAME1}"))
            self.assertTrue((self.root / f"{name}.old" / "bin" / "python").is_file())
        self.assertTrue(all(row["swap"]["readback"] for row in result["venvs"]))
        pip = [c for c in installs if c[0].endswith(f"venv-a.{NAME1}/bin/python")]
        self.assertEqual(len(pip), 2)  # pip runs by the new venv's own python, never the live link
        self.assertFalse(
            any(c[:1] == [str(self.root / "venv-a" / "bin" / "python")] and "pip" in c for c in self.commands)
        )

    def test_the_pinned_packages_of_the_project_are_installed_beside_the_tools(self):
        """RageWars pins splat64 and spimdisasm in requirements.txt: a plain deploy ships them, no hand reinstall."""
        self.requirements = "# make extract\nsplat64[mips]==0.50.0\nspimdisasm==1.42.4  # x\n"
        result, code, findings = self.deploy()
        self.assertEqual((code, findings), (0, []))
        self.assertEqual(result["packages"], ["splat64[mips]==0.50.0", "spimdisasm==1.42.4"])
        self.assertEqual(result["project_revision"], "c" * 40)
        install = [c for c in self.commands if "install" in c][-1]
        self.assertEqual(install[-2:], ["splat64[mips]==0.50.0", "spimdisasm==1.42.4"])

    def test_the_requirements_come_from_the_project_main_or_the_named_revision_never_the_tree(self):
        (self.project / "requirements.txt").write_text("stale-tree==1\n")  # a work tree file is never read
        self.requirements = "splat64==0.50.0\n"
        result, _, _ = self.deploy()
        self.assertEqual((result["packages"], self.asked[-1][1:]), (["splat64==0.50.0"], ("origin", "main", None)))
        self.deploy(extra={"project_sha": "9" * 40})
        self.assertEqual(self.asked[-1][3], "9" * 40)
        with self.assertRaises(deploy.DeployError) as caught:
            self.deploy(extra={"project_sha": "main"})
        self.assertEqual(caught.exception.key, "deploy.sha")

    def test_the_land_remote_and_branch_name_the_projects_main(self):
        document = json.loads((self.root / "seat.json").read_text())
        document["land"] = {"remote": "github", "branch": "trunk"}
        (self.root / "seat.json").write_text(json.dumps(document))
        self.profile = load_profile(self.root / "seat.json")
        self.deploy()
        self.assertEqual(self.asked[-1][1:], ("github", "trunk", None))

    def test_a_project_package_that_is_not_an_exact_pin_refuses_before_anything_is_installed(self):
        self.requirements = "splat64>=0.50\n"
        with self.assertRaises(deploy.DeployError) as caught:
            self.deploy()
        self.assertEqual((caught.exception.key, self.commands), ("deploy.requirements", []))

    def test_a_venv_that_cannot_import_a_declared_package_is_a_failed_deploy(self):
        self.requirements = "splat64==0.50.0\n"
        self.fail_on = deploy.IMPORT_CHECK  # the import check's own source: a bare "-c" matched any run path holding it
        result, code, findings = self.deploy()
        self.assertEqual(code, 1)
        self.assertEqual([f["key"] for f in findings], ["deploy.smoke"])
        self.assertIn("import splat64==0.50.0", findings[0]["message"])
        self.assertNotIn("swap", result["venvs"][0])

    def test_the_import_check_imports_each_declared_distribution_and_fails_on_a_missing_one(self):
        def check(*specs):
            return subprocess.run(
                [sys.executable, "-I", "-c", deploy.IMPORT_CHECK, *specs], capture_output=True, text=True, check=False
            )

        ok = check("pip==1.0", "PIP[x]==1")
        self.assertEqual((ok.returncode, ok.stdout.count("imports pip")), (0, 2), ok.stderr)
        bad = check("no-such-distribution==1")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("installs no importable module", bad.stderr)

    def test_the_import_check_skips_the_stray_top_level_modules_of_a_distribution(self):
        """splat64 installs `splat` and a `dist` that cannot be imported: the package is the module it is named for."""
        site = self.root / "site"
        (site / "fakepkg" / "__init__.py").parent.mkdir(parents=True)
        (site / "fakepkg" / "__init__.py").write_text("")
        (site / "dist").mkdir()
        (site / "dist" / "__init__.py").write_text("raise ImportError('stray')")
        info = site / "fakepkg64-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: fakepkg64\nVersion: 1.0\n")
        (info / "top_level.txt").write_text("fakepkg\ndist\n")
        done = subprocess.run(
            [sys.executable, "-c", deploy.IMPORT_CHECK, "fakepkg64==1.0"],
            env={**os.environ, "PYTHONPATH": str(site)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "fakepkg64==1.0 imports fakepkg"), done.stderr)

    def test_a_stale_detached_tree_cannot_drop_the_projects_requirements(self):
        """The 10-29 defect: the tree sat detached at a commit without requirements.txt while main had it."""
        origin, tree = self.root / "origin", self.root / "tree"
        git = lambda *argv, cwd=origin: subprocess.run(  # noqa: E731
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv], cwd=cwd, check=True, capture_output=True
        )
        origin.mkdir()
        git("init", "-q", "-b", "main")
        (origin / "a").write_text("1")
        git("add", "a")
        git("commit", "-qm", "old")
        git("clone", "-q", str(origin), str(tree), cwd=self.root)
        git("checkout", "-q", "--detach", cwd=tree)
        (origin / "requirements.txt").write_text("splat64==0.50.0\n")
        git("add", "requirements.txt")
        git("commit", "-qm", "new")
        newest = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=origin, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertFalse((tree / "requirements.txt").exists())
        self.assertEqual(deploy.requirements_at(tree, "origin", "main", None), (newest, "splat64==0.50.0\n"))
        self.assertEqual(deploy.requirements_at(tree, "origin", "main", newest)[1], "splat64==0.50.0\n")
        old = subprocess.run(
            ["git", "rev-parse", "HEAD~1"], cwd=origin, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(deploy.requirements_at(tree, "origin", "main", old)[1], "")
        with self.assertRaises(deploy.DeployError) as caught:
            deploy.requirements_at(tree, "origin", "main", "f" * 40)
        self.assertEqual(caught.exception.key, "deploy.requirements")

    def test_a_project_file_naming_another_tool_version_never_blocks_a_deploy(self):
        """Game projects build standalone and pin none of our tools: a tool release never asks a project to move."""
        self.pin_file(unbake="0.4.0")
        _, code, findings = self.deploy()
        self.assertEqual((code, findings), (0, []))

    def test_only_full_commit_shas_and_real_venvs_are_accepted(self):
        for bad in ("main", "abc1234", "https://x/y.whl"):
            with self.subTest(bad=bad), self.assertRaises(deploy.DeployError) as caught:
                self.deploy(sha=bad)
            self.assertEqual(caught.exception.key, "deploy.sha")
        with self.assertRaises(deploy.DeployError) as caught:
            self.deploy(package="not-ours")
        self.assertEqual(caught.exception.key, "deploy.package")
        with self.assertRaises(deploy.DeployError) as caught:
            self.deploy(venvs=("missing",))
        self.assertEqual(caught.exception.key, "deploy.venv")

    def test_a_failing_install_and_a_failing_smoke_are_findings(self):
        self.fail_on = "pip install"
        _, code, findings = self.deploy()
        self.assertEqual((code, findings[0]["key"]), (1, "deploy.install"))
        self.assertEqual(os.path.realpath(self.root / "venv-a"), str(self.root / "venv-a.old"))  # live untouched
        self.assertFalse((self.root / f"venv-a.{NAME1}").exists())
        self.fail_on = "dependencies"
        _, code, findings = self.deploy()
        self.assertEqual((code, findings[0]["key"]), (1, "deploy.smoke"))
        self.assertEqual(os.path.realpath(self.root / "venv-a"), str(self.root / "venv-a.old"))
        self.assertFalse((self.root / f"venv-a.{NAME1}").exists())

    def test_the_fresh_tree_goes_read_only_after_the_smoke_gate_and_before_the_link_flips(self):
        live, order = self.root / "venv-a", []
        old = os.path.realpath(live)

        def runner(argv, cwd=None):
            if argv[0] == "chmod":
                order.append(("chmod", argv[1:], os.path.realpath(live) == old))  # the link still names the old tree
            elif argv[0].endswith("decomp") and argv[1] == "dependencies":
                order.append(("smoke", os.path.realpath(live) == old))
            return self.runner(argv, cwd)

        real_point = deploy.point

        def point(*args):
            order.append(("flip",))
            return real_point(*args)

        with mock.patch.object(deploy, "point", point):
            result, code, findings = deploy.deploy(
                self.profile, "abu-decomp-64", D1, [live], run=runner, requirements_of=self.requirements_of
            )
        self.assertEqual((code, findings), (0, []))
        kinds = [o[0] for o in order]
        self.assertEqual(kinds, ["smoke", "chmod", "flip", "smoke"])  # gate, read-only, flip, readback smoke
        fresh = result["venvs"][0]["versioned"]
        self.assertEqual(order[1], ("chmod", ["-R", "a-w", fresh], True))

    def test_a_failed_smoke_never_seals_and_a_failed_seal_never_flips(self):
        self.fail_on = "dependencies"
        _, code, _ = self.deploy()
        self.assertEqual(code, 1)
        self.assertFalse([c for c in self.commands if c[0] == "chmod"])
        self.fail_on = "chmod"
        old = os.path.realpath(self.root / "venv-a")
        _, code, findings = self.deploy()
        self.assertEqual((code, findings[0]["key"]), (1, "deploy.seal"))
        self.assertEqual(os.path.realpath(self.root / "venv-a"), old)

    def test_seal_makes_a_real_tree_unwritable_and_unseal_removes_it(self):
        tree = self.root / "sealed"
        (tree / "lib" / "site").mkdir(parents=True)
        (tree / "lib" / "site" / "m.py").write_text("x = 1\n")
        self.assertTrue(deploy.seal(tree, deploy.run_command)["ok"])
        for path in (tree, tree / "lib" / "site", tree / "lib" / "site" / "m.py"):
            self.assertFalse(os.access(path, os.W_OK) and os.geteuid() != 0, path)
        with self.assertRaises(PermissionError):
            (tree / "new.txt").write_text("no")
        deploy.unseal(tree)
        self.assertFalse(tree.exists())

    def test_a_redeploy_of_one_commit_gets_its_own_versioned_dir(self):
        self.deploy()
        result, code, _ = self.deploy()
        self.assertEqual(code, 0)
        self.assertEqual(result["venvs"][0]["versioned"], str(self.root / f"venv-a.{NAME1}-2"))
        self.assertEqual(os.path.realpath(self.root / "venv-a"), str(self.root / f"venv-a.{NAME1}-2"))
        self.assertEqual(result["venvs"][0]["swap"]["previous"], str(self.root / f"venv-a.{NAME1}"))
        self.assertTrue((self.root / f"venv-a.{NAME1}" / "bin" / "python").is_file())

    def test_a_deploy_resyncs_every_work_tree_with_the_new_venv_and_skips_a_held_one(self):
        trees = [self.root / name for name in ("lane-a", "lane-held", "lane-bad", "no-makefile")]
        for tree in trees[:3]:
            tree.mkdir()
            (tree / "Makefile").write_text("")
        trees[3].mkdir()
        calls = []

        def runner(argv, cwd=None):
            if argv[:3] == ["git", "worktree", "list"]:
                return 0, "".join(f"worktree {t}\nHEAD {'0' * 40}\n\n" for t in trees)
            if "sync" in argv:
                calls.append((argv, cwd))
                return (1, "Refused: a literals_ fact names no function") if cwd == trees[2] else (0, "")
            return self.runner(argv, cwd)

        held = lambda tree: [4242] if tree == trees[1] else []  # noqa: E731
        with mock.patch.object(deploy, "holders", held):
            result, code, findings = deploy.deploy(
                self.profile,
                "abu-decomp-64",
                D1,
                [self.root / "venv-a"],
                run=runner,
                requirements_of=self.requirements_of,
            )
        self.assertEqual((code, findings), (0, []))  # a refused or held tree never fails the deploy
        live = self.root / "venv-a"  # the link, already swapped: the files name the live path, never a versioned one
        self.assertEqual(
            [(Path(argv[0]).parent.parent, argv[-2:], cwd) for argv, cwd in calls],
            [(live, ["sync", "--makefile"], trees[0]), (live, ["sync", "--makefile"], trees[2])],
        )
        self.assertEqual(
            [(Path(r["tree"]).name, r["outcome"]) for r in result["venvs"][0]["resync"]],
            [("lane-a", "synced"), ("lane-held", "skipped"), ("lane-bad", "refused")],
        )
        self.assertEqual(result["venvs"][0]["resync"][1]["held_by"], [4242])
        self.assertIn("literals_", result["venvs"][0]["resync"][2]["tail"])

    def test_holders_names_the_processes_working_inside_a_tree(self):
        tree = self.root / "busy"
        (tree / "deep").mkdir(parents=True)
        child = subprocess.Popen(["sleep", "30"], cwd=tree / "deep")
        self.addCleanup(child.kill)
        self.assertEqual(deploy.holders(tree), [child.pid])
        self.assertEqual(deploy.holders(self.root / "nowhere"), [])

    def test_a_real_directory_is_refused_not_migrated(self):
        self.make_venv(self.root / "venv-c")
        with self.assertRaises(deploy.DeployError) as caught:
            self.deploy(venvs=("venv-c",))
        self.assertEqual(caught.exception.key, "deploy.venv")

    def test_cache_report_reads_unbake_stage_telemetry(self):
        stages = {
            "path": ["score"],
            "cache": {},
            "children": [
                {"path": ["score", "context"], "cache": {"build": [0, 3], "view": [2, 0]}, "children": []},
                {"path": ["score", "cold"], "cache": {"build": [0, 0]}, "children": []},
            ],
        }
        report = tools.cache_report(stages)
        self.assertEqual(report, {"score > context": {"build": [0, 3], "view": [2, 0]}})
        summary = tools.cache_summary(report)
        self.assertEqual((summary["hits"], summary["misses"]), (2, 3))
        self.assertEqual(summary["stages"]["score > context"]["build"], {"hits": 0, "misses": 3})
        tools.CACHE_LOG[:] = [
            {"call": "unbake score", "report": report},
            {"call": "api build_context", "report": None},
        ]
        drained = tools.drain_cache()
        self.assertEqual((drained["hits"], drained["misses"], drained["unexposed"]), (2, 3, ["api build_context"]))
        self.assertEqual(tools.drain_cache()["calls"], [])


if __name__ == "__main__":
    unittest.main()


class CarryTests(DeployTests):
    """The carry-over of the packages the deployed one does not replace."""

    def test_the_package_and_the_projects_own_packages_are_not_carried(self):
        self.requirements = "splat64[mips]==0.50.0\n"
        self.deploy(package="unbake", sha=U2)
        asked = next(c for c in self.commands if c[1:2] == ["-c"])
        self.assertEqual(asked[0], str(self.root / "venv-a" / "bin" / "python"))  # the live venv lists itself
        self.assertEqual(asked[3:], ["unbake", "splat64"])
        install = [c for c in self.commands if "install" in c][-1]
        self.assertEqual(install[-2:], [f"unbake[dev] @ git+{URL['unbake']}@{U2}", "splat64[mips]==0.50.0"])

    def test_the_carried_set_is_read_from_carry_from_and_the_scratch_venv_is_never_run(self):
        """ed1c85a: the rig's `venv-decomp-stage` is scratch the deploy recreates; the live toolkit link is carried."""
        live = self.root / "toolkit"
        self.make_venv(self.root / "toolkit.real")
        live.symlink_to(self.root / "toolkit.real")
        scratch = self.root / "venv-a"
        result, code, findings = self.deploy(extra={"carry_from": live})
        self.assertEqual((code, findings), (0, []))
        asked = next(c for c in self.commands if c[1:2] == ["-c"])
        self.assertEqual(asked[0], str(live / "bin" / "python"))
        create = next(c for c in self.commands if "venv" in c and "-m" in c)
        self.assertEqual(create[0], str(live / "bin" / "python"))
        self.assertFalse(any(c[0] == str(scratch / "bin" / "python") for c in self.commands if "pip" not in c))
        self.assertEqual(result["venvs"][0]["carried"], CARRIED)
        self.assertEqual(os.path.realpath(scratch), str(self.root / f"venv-a.{NAME1}"))  # the scratch link is swapped

    def test_a_carry_from_that_is_not_a_venv_refuses_before_anything_runs(self):
        with self.assertRaises(deploy.DeployError) as caught:
            self.deploy(extra={"carry_from": self.root / "nowhere"})
        self.assertEqual(caught.exception.key, "deploy.venv")
        self.assertEqual(self.commands, [])

    def test_a_live_file_install_refuses_naming_the_origin_and_the_live_link(self):
        """The rig's live unbake was installed from file:///home/abu/runs/char-de-3/src by a run."""
        live = self.root / "toolkit"
        self.make_venv(live)
        origin = "unbake is installed from file:///home/abu/runs/char-de-3/src, not from a git commit"

        def run(argv, cwd=None):
            return (1, origin) if argv[1:2] == ["-c"] else (0, "")

        with self.assertRaises(deploy.DeployError) as caught:
            deploy.deploy(
                self.profile,
                "abu-decomp-64",
                D1,
                [self.root / "venv-a"],
                carry_from=live,
                run=run,
                requirements_of=self.requirements_of,
            )
        self.assertEqual(caught.exception.key, "deploy.carry")
        self.assertIn(f"packages installed in {live}", str(caught.exception))
        self.assertIn("file:///home/abu/runs/char-de-3/src", str(caught.exception))

    def carry_script(self, **dists):
        """Runs deploy.CARRY (the live venv's python) over fake dist-infos; each dist is its direct_url.json or None."""
        site = self.root / "site"
        for name, origin in dists.items():
            info = site / f"{name}-1.0.dist-info"
            info.mkdir(parents=True)
            (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
            if origin is not None:
                (info / "direct_url.json").write_text(json.dumps(origin))
        env = {**os.environ, "PYTHONPATH": str(site)}
        done = subprocess.run([sys.executable, "-S", "-c", deploy.CARRY], env=env, capture_output=True, text=True)
        return done.returncode, done.stdout, done.stderr

    def test_a_release_wheel_url_install_is_carried_as_that_url(self):
        """rig-20261031T184313Z: abucache was installed from a release wheel URL and deploy.carry refused it."""
        wheel = "https://github.com/AbuN64/cache-64/releases/download/v0.3.3/abucache-0.3.3-py3-none-any.whl"
        code, out, err = self.carry_script(
            abucache={"url": wheel, "archive_info": {"hash": "sha256=0"}},
            gitpkg={"url": URL["unbake"], "vcs_info": {"vcs": "git", "commit_id": U1}},
            plain=None,
        )
        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        self.assertEqual(rows, sorted([f"abucache @ {wheel}", f"gitpkg @ git+{URL['unbake']}@{U1}", "plain==1.0"]))

    def test_a_file_url_or_an_editable_install_refuses_naming_the_origin(self):
        for origin in (
            {"url": "file:///home/abu/runs/char-de-3/src", "dir_info": {}},
            {"url": "file:///home/abu/runs/char-de-3/src", "dir_info": {"editable": True}},
            {"url": "file:///x/unbake-0.1-py3-none-any.whl", "archive_info": {}},
        ):
            with self.subTest(origin=origin):
                code, _, err = self.carry_script(unbake=origin)
                self.assertNotEqual(code, 0)
                self.assertIn(f"unbake is installed from {origin['url']}", err)
                self.root.joinpath("site").rename(self.root / f"site-{abs(hash(str(origin)))}")

    def test_a_venv_that_cannot_be_listed_refuses_before_anything_is_built(self):
        self.fail_on = "metadata.distributions"
        with self.assertRaises(deploy.DeployError) as caught:
            self.deploy()
        self.assertEqual(caught.exception.key, "deploy.carry")
        self.assertFalse([c for c in self.commands if "venv" in c])

    def test_only_the_named_tools_checkout_moves(self):
        wanted, findings = deploy.checkout_steps(self.profile, "unbake", U2)
        self.assertEqual((wanted, findings), ({}, []))  # the seat names no checkout


class NameTests(DeployTests):
    """The build name identifies every package the build holds: the 10-31 collision of two deploys into one toolkit."""

    def name(self, package="abu-decomp-64", sha=D1, source="venv-a"):
        return deploy.name(
            self.profile, package, sha, self.root / source, run=self.runner, requirements_of=self.requirements_of
        )

    def test_the_name_is_the_content_of_the_whole_build_not_the_deployed_commit(self):
        first, _, _ = self.name(package="unbake", sha=U2)
        self.carried = [row.replace(R1, "4" * 40) for row in CARRIED]  # another package moved: another build
        second, _, _ = self.name(package="unbake", sha=U2)
        self.assertNotEqual(first["name"], second["name"])
        self.assertEqual(first["name"], deploy.content_name(first["requirements"], first["carried"]))
        self.assertNotIn(U2[:12], first["name"])

    def test_one_content_has_one_name_whichever_order_it_is_listed_in(self):
        self.assertEqual(deploy.content_name(["b", "a"], ["c"]), deploy.content_name(["a"], ["c", "b"]))
        self.assertNotEqual(deploy.content_name(["a"], ["c"]), deploy.content_name(["a"], ["d"]))

    def test_the_name_of_a_deploy_is_the_name_the_name_action_answers_before_it(self):
        answered, code, findings = self.name()
        self.assertEqual((code, findings), (0, []))
        result, _, _ = self.deploy()
        self.assertEqual(result["venvs"][0]["name"], answered["name"])
        self.assertEqual(result["venvs"][0]["versioned"], str(self.root / f"venv-a.{answered['name']}"))

    def test_a_deploy_giving_another_name_than_the_expected_one_refuses_before_building(self):
        answered = self.name()[0]["name"]
        before = {entry.name for entry in self.root.iterdir()}
        with self.assertRaises(deploy.DeployError) as refused:
            self.deploy(extra={"expect_name": "0" * 12})
        self.assertEqual(refused.exception.key, "deploy.name")
        self.assertEqual({entry.name for entry in self.root.iterdir()}, before)
        result, code, _ = self.deploy(extra={"expect_name": answered})
        self.assertEqual((code, result["venvs"][0]["name"]), (0, answered))

    def test_two_packages_deployed_after_each_other_get_two_names_and_a_repeat_keeps_its_own(self):
        decomp = self.name()[0]["name"]
        unbake = self.name(package="unbake", sha=U2)[0]["name"]
        self.assertNotEqual(decomp, unbake)
        self.assertEqual(unbake, self.name(package="unbake", sha=U2)[0]["name"])

    def test_the_name_action_refuses_a_bad_sha_package_or_source_and_builds_nothing(self):
        for kwargs in ({"sha": "abc"}, {"package": "nope"}, {"source": "nowhere"}):
            with self.assertRaises(deploy.DeployError):
                self.name(**kwargs)
        self.assertFalse([c for c in self.commands if "install" in c])
