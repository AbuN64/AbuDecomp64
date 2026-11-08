"""enhanced.sibling finds RageWarsEnhanced beside the repository's own top level, never beside the registered checkout
that a worktree's git common dir points at."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from abu_decomp import DecompError, enhanced


def git(path, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class SiblingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.registered = root / "registered" / "AbuDecomp64"
        self.registered.mkdir(parents=True)
        git(self.registered, "init", "-q")
        git(self.registered, "commit", "-q", "--allow-empty", "-m", "base")
        self.run_tree = root / "run" / "AbuDecomp64"
        git(self.registered, "worktree", "add", "-q", "-b", "card", str(self.run_tree))

    def test_worktree_finds_the_sibling_beside_its_own_top_level(self):
        (self.run_tree.parent / "RageWarsEnhanced").mkdir()
        (self.registered.parent / "RageWarsEnhanced").mkdir()
        self.assertEqual(enhanced.sibling(self.run_tree), self.run_tree.parent / "RageWarsEnhanced")

    def test_sibling_beside_only_the_registered_checkout_is_not_used(self):
        (self.registered.parent / "RageWarsEnhanced").mkdir()
        with self.assertRaises(DecompError) as raised:
            enhanced.sibling(self.run_tree)
        self.assertEqual(raised.exception.key, "enhanced.missing")
        self.assertIn(str(self.run_tree.parent / "RageWarsEnhanced"), str(raised.exception))

    def test_missing_sibling_is_refused_naming_the_path(self):
        with self.assertRaises(DecompError) as raised:
            enhanced.sibling(self.registered)
        self.assertEqual(raised.exception.key, "enhanced.missing")
        self.assertIn(str(self.registered.parent / "RageWarsEnhanced"), str(raised.exception))

    def test_registered_checkout_finds_its_own_sibling(self):
        (self.registered.parent / "RageWarsEnhanced").mkdir()
        self.assertEqual(enhanced.sibling(self.registered), self.registered.parent / "RageWarsEnhanced")


if __name__ == "__main__":
    unittest.main()
