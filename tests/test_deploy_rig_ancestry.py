"""READBACK refuses a tool installed at a commit that is not an ancestor of its source's main, naming the tool and the
commit, and accepts one that is (behind or current).

Found 11-02 by AbuN64 on rig-20261102T062841Z: the rig toolkit held unbake 55c9a21, 48 commits behind main b7675b8,
and cake-taste-64 37a0314 behind 4be5381, because the deploy carried every tool as installed. A tool's own land deploys
its own toolkit, so only this deploy moves them, and only to a landed commit. The sources are local repositories the
git seam's url rewriting points at (ReadbackCase); `deploy.on_main` is also checked with its run seam mocked."""

import subprocess
import unittest

from test_deploy_rig import ReadbackCase, git

from abu_decomp import deploy

HANDGATE = "blob-fuzzy: report every blob row as exact, fuzzy or untyped"


class AncestryTests(ReadbackCase):
    def source(self, tool):
        return self.root / "sources" / deploy.PACKAGES[tool].rsplit("/", 1)[-1]

    def commit(self, tool, message, branch=None):
        remote = self.source(tool)
        if branch:
            git("checkout", "-q", "-b", branch, cwd=remote)
        git("commit", "-q", "--allow-empty", "-m", message, cwd=remote)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=remote, capture_output=True, text=True, check=True)
        if branch:
            git("checkout", "-q", "main", cwd=remote)
        return head.stdout.strip()

    def install(self, tool, commit):
        subprocess.run(["chmod", "-R", "u+w", str(self.venv)], check=True)
        self.dist(tool, commit)
        self.seal()

    def test_a_tool_installed_from_a_branch_off_its_main_refuses_naming_the_tool_and_the_commit(self):
        off = self.commit("unbake", HANDGATE, branch="handgate/blob-fuzzy")
        self.install("unbake", off)
        done = self.readback()
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn(f"readback-tool: unbake installed {off} landed {self.landed['unbake']} off-main", done.stdout)
        self.assertIn(
            f"unbake is installed at {off}, which is not on the main of {deploy.PACKAGES['unbake']}", done.stderr
        )

    def test_a_tool_installed_at_an_ancestor_of_its_main_is_accepted_as_behind(self):
        old = self.landed["unbake"]
        self.landed["unbake"] = self.commit("unbake", "landed since the build")
        done = self.readback()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"readback-tool: unbake installed {old} landed {self.landed['unbake']} behind", done.stdout)
        for tool in ("recipemaker", "cake-taste-64"):
            self.assertIn(
                f"readback-tool: {tool} installed {self.landed[tool]} landed {self.landed[tool]} current", done.stdout
            )


class OnMainTests(unittest.TestCase):
    URL = "https://example.lan/tool"

    def run_seam(self, answers):
        calls = []

        def run(argv, cwd=None):
            calls.append(argv)
            verb = argv[3] if argv[1] == "-C" else argv[1]
            return answers.get(verb, (0, ""))

        return calls, run

    def test_an_ancestor_is_on_main_and_a_commit_the_fetch_does_not_hold_is_not(self):
        calls, run = self.run_seam({})
        self.assertTrue(deploy.on_main(self.URL, "5" * 40, run))
        self.assertEqual(calls[1][3:], ["fetch", "-q", "--filter=tree:0", self.URL, "HEAD"])  # commits only, no blobs
        self.assertEqual(calls[2][3:], ["merge-base", "--is-ancestor", "5" * 40, "FETCH_HEAD"])
        calls, run = self.run_seam({"merge-base": (128, "fatal: Not a valid commit name")})
        self.assertFalse(deploy.on_main(self.URL, "5" * 40, run))

    def test_a_source_that_cannot_be_fetched_refuses(self):
        calls, run = self.run_seam({"fetch": (128, "fatal: unreachable")})
        with self.assertRaises(deploy.DeployError) as refused:
            deploy.on_main(self.URL, "5" * 40, run)
        self.assertEqual(refused.exception.key, "deploy.landed")
        self.assertIn(f"{self.URL}: ", str(refused.exception))
        self.assertIn("fatal: unreachable", str(refused.exception))
        self.assertEqual(len(calls), 2)  # nothing is asked of a fetch that failed


if __name__ == "__main__":
    unittest.main()
