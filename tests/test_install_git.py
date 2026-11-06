"""install_identity + bounded git upgrade helpers."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pangpang import __version__
from pangpang.install_git import (
    apply_upgrade, install_identity, plan_upgrade, require_target_commit, tracking_ref,
)
from pangpang.store import Fault


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo():
    root = Path(tempfile.mkdtemp())
    _git(root, "init")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README").write_text("a\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-m", "one")
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return root, head


class RequireTarget(unittest.TestCase):
    def test_accepts_full_sha(self):
        sha = "a" * 40
        self.assertEqual(require_target_commit(sha), sha)

    def test_rejects_short(self):
        with self.assertRaises(Fault) as raised:
            require_target_commit("abc")
        self.assertEqual(raised.exception.code, "invalid_target_commit")


class TrackingRef(unittest.TestCase):
    def test_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PANGPANG_UPGRADE_REMOTE", None)
            os.environ.pop("PANGPANG_UPGRADE_BRANCH", None)
            self.assertEqual(tracking_ref(), ("origin", "main"))

    def test_rejects_injection(self):
        with mock.patch.dict(os.environ, {"PANGPANG_UPGRADE_REMOTE": "origin;rm", "PANGPANG_UPGRADE_BRANCH": "main"}):
            with self.assertRaises(Fault) as raised:
                tracking_ref()
            self.assertEqual(raised.exception.code, "invalid_upgrade_tracking_ref")


class InstallIdentity(unittest.TestCase):
    def test_prefers_baked_build(self):
        fake = mock.Mock(BUILD_COMMIT="b" * 40, BUILD_REF="v1", BUILT_AT="T")
        with mock.patch.dict("sys.modules", {"pangpang._build": fake}):
            # re-import path goes through build_identity import inside function
            with mock.patch("pangpang.install_git.build_identity", return_value=("b" * 40, "v1", "T")):
                ident = install_identity()
        self.assertEqual(ident["source"], "build")
        self.assertEqual(ident["commit"], "b" * 40)
        self.assertEqual(ident["version"], __version__)

    def test_unknown_without_git_or_build(self):
        with mock.patch("pangpang.install_git.build_identity", return_value=("unknown", "unknown", "unknown")):
            with mock.patch("pangpang.install_git.install_root", return_value=None):
                ident = install_identity()
        self.assertEqual(ident["source"], "unknown")
        self.assertEqual(ident["commit"], "unknown")


class ApplyUpgrade(unittest.TestCase):
    def test_already_current(self):
        root, head = _repo()
        # Bare remote clone as origin/main
        remote = Path(tempfile.mkdtemp())
        subprocess.run(["git", "clone", "--bare", str(root), str(remote)], check=True, capture_output=True)
        _git(root, "remote", "add", "origin", str(remote))
        _git(root, "fetch", "origin", "master")
        # Some git defaults to master; rename remote tip to main for tracking_ref
        subprocess.run(["git", "--git-dir", str(remote), "branch", "-M", "main"], check=True)
        _git(root, "fetch", "origin", "main")
        result = apply_upgrade(head, root=root)
        self.assertEqual(result["status"], "already_current")
        self.assertEqual(result["commit"], head)

    def test_updates_to_tip(self):
        root, head = _repo()
        remote = Path(tempfile.mkdtemp())
        subprocess.run(["git", "clone", "--bare", str(root), str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "--git-dir", str(remote), "branch", "-M", "main"], check=True)
        _git(root, "remote", "add", "origin", str(remote))
        # Advance remote
        work = Path(tempfile.mkdtemp())
        subprocess.run(["git", "clone", str(remote), str(work)], check=True, capture_output=True)
        _git(work, "config", "user.email", "t@example.com")
        _git(work, "config", "user.name", "t")
        _git(work, "checkout", "main")
        (work / "README").write_text("b\n", encoding="utf-8")
        _git(work, "add", "README")
        _git(work, "commit", "-m", "two")
        tip = subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip()
        _git(work, "push", "origin", "main")
        result = apply_upgrade(tip, root=root)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["commit"], tip)
        self.assertEqual(result["previous"], head)
        now = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        self.assertEqual(now, tip)

    def test_rejects_off_tracking_commit(self):
        root, head = _repo()
        remote = Path(tempfile.mkdtemp())
        subprocess.run(["git", "clone", "--bare", str(root), str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "--git-dir", str(remote), "branch", "-M", "main"], check=True)
        _git(root, "remote", "add", "origin", str(remote))
        other = Path(tempfile.mkdtemp())
        _git(other, "init")
        _git(other, "config", "user.email", "t@example.com")
        _git(other, "config", "user.name", "t")
        (other / "x").write_text("x\n", encoding="utf-8")
        _git(other, "add", "x")
        _git(other, "commit", "-m", "other")
        alien = subprocess.check_output(["git", "-C", str(other), "rev-parse", "HEAD"], text=True).strip()
        # Fetch so object may or may not exist; alien is not fetched — unavailable or not on tip
        with self.assertRaises(Fault) as raised:
            apply_upgrade(alien, root=root)
        self.assertIn(raised.exception.code, {
            "target_commit_unavailable", "target_commit_not_on_tracking_ref", "git_fetch_failed",
        })


class PlanUpgrade(unittest.TestCase):
    def test_plan_reports_tip(self):
        root, head = _repo()
        remote = Path(tempfile.mkdtemp())
        subprocess.run(["git", "clone", "--bare", str(root), str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "--git-dir", str(remote), "branch", "-M", "main"], check=True)
        _git(root, "remote", "add", "origin", str(remote))
        plan = plan_upgrade(root=root)
        self.assertEqual(plan["current_commit"], head)
        self.assertEqual(plan["tip_commit"], head)
        self.assertTrue(plan["already_current"])
        self.assertEqual(plan["tracking_ref"], "origin/main")


if __name__ == "__main__":
    unittest.main()
