"""install_identity + bounded git upgrade helpers."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pangpang import __version__
from pangpang.install_git import (
    apply_upgrade, install_identity, plan_upgrade, require_target_commit, restart_argv, tracking_ref,
)
from pangpang.store import Fault
from pangpang import install_git as install_git_mod


def _clear_identity_cache():
    install_git_mod._IDENTITY_CACHE["value"] = None
    install_git_mod._IDENTITY_CACHE["at"] = 0.0


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
        _clear_identity_cache()
        fake = mock.Mock(BUILD_COMMIT="b" * 40, BUILD_REF="v1", BUILT_AT="T")
        with mock.patch.dict("sys.modules", {"pangpang._build": fake}):
            # re-import path goes through build_identity import inside function
            with mock.patch("pangpang.install_git.build_identity", return_value=("b" * 40, "v1", "T")):
                ident = install_identity()
        self.assertEqual(ident["source"], "build")
        self.assertEqual(ident["commit"], "b" * 40)
        self.assertEqual(ident["version"], __version__)

    def test_unknown_without_git_or_build(self):
        _clear_identity_cache()
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


class RestartArgv(unittest.TestCase):
    def test_rebuilds_module_form(self):
        from pangpang.install_git import restart_argv
        with mock.patch("pangpang.install_git.sys") as fake_sys:
            fake_sys.executable = "/usr/bin/python3"
            fake_sys.argv = ["/usr/bin/python3", "-m", "pangpang", "--data", "/tmp/data", "--origin", "https://x"]
            argv = restart_argv("pangpang")
        self.assertEqual(argv[:5], ["/usr/bin/python3", "-m", "pangpang", "--data", "/tmp/data"])
        self.assertIn("--origin", argv)


class TrackingRefHardening(unittest.TestCase):
    def test_rejects_leading_dash_remote(self):
        with mock.patch.dict(os.environ, {"PANGPANG_UPGRADE_REMOTE": "--upload-pack", "PANGPANG_UPGRADE_BRANCH": "main"}):
            with self.assertRaises(Fault) as raised:
                tracking_ref()
            self.assertEqual(raised.exception.code, "invalid_upgrade_tracking_ref")

    def test_rejects_absolute_branch_path(self):
        with mock.patch.dict(os.environ, {"PANGPANG_UPGRADE_REMOTE": "origin", "PANGPANG_UPGRADE_BRANCH": "/tmp/helper"}):
            with self.assertRaises(Fault) as raised:
                tracking_ref()
            self.assertEqual(raised.exception.code, "invalid_upgrade_tracking_ref")


class IdentityIsolation(unittest.TestCase):
    def test_missing_git_binary_returns_unknown(self):
        root, _head = _repo()
        with mock.patch("pangpang.install_git.build_identity", return_value=("unknown", "unknown", "unknown")):
            with mock.patch("pangpang.install_git.install_root", return_value=root):
                with mock.patch("pangpang.install_git.subprocess.run", side_effect=FileNotFoundError("git")):
                    from pangpang import install_git as ig
                    ig._IDENTITY_CACHE["value"] = None
                    ident = install_identity()
        self.assertEqual(ident["source"], "unknown")
        self.assertEqual(ident["commit"], "unknown")


class TipNamespace(unittest.TestCase):
    def test_same_name_tag_does_not_override_remote_tip(self):
        root, head = _repo()
        remote = Path(tempfile.mkdtemp())
        subprocess.run(["git", "clone", "--bare", str(root), str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "--git-dir", str(remote), "branch", "-M", "main"], check=True)
        _git(root, "remote", "add", "origin", str(remote))
        # Side branch commit C not on main
        _git(root, "checkout", "-b", "side")
        (root / "README").write_text("side\n", encoding="utf-8")
        _git(root, "add", "README")
        _git(root, "commit", "-m", "side")
        alien = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        _git(root, "checkout", "master")
        # Some inits use master; ensure we are on the commit that matches remote main
        _git(root, "reset", "--hard", head)
        _git(root, "tag", "origin/main", alien)
        with self.assertRaises(Fault) as raised:
            apply_upgrade(alien, root=root)
        self.assertEqual(raised.exception.code, "target_commit_not_on_tracking_ref")


class FetchHeadTip(unittest.TestCase):
    def test_fetch_head_tip_ignores_stale_remote_ref(self):
        root, head = _repo()
        remote = Path(tempfile.mkdtemp())
        subprocess.run(["git", "clone", "--bare", str(root), str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "--git-dir", str(remote), "branch", "-M", "main"], check=True)
        _git(root, "remote", "add", "origin", str(remote))
        _git(root, "fetch", "origin", "main")
        # Advance remote tip
        work = Path(tempfile.mkdtemp())
        subprocess.run(["git", "clone", str(remote), str(work)], check=True, capture_output=True)
        _git(work, "config", "user.email", "t@example.com")
        _git(work, "config", "user.name", "t")
        _git(work, "checkout", "main")
        (work / "README").write_text("c\n", encoding="utf-8")
        _git(work, "add", "README")
        _git(work, "commit", "-m", "three")
        tip = subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip()
        _git(work, "push", "origin", "main")
        # Point remote.fetch away from main so refs/remotes/origin/main stays stale (at head)
        _git(root, "remote", "set-branches", "origin", "release")
        # Stale remotes/origin/main still equals old head; FETCH_HEAD after fetch must be tip
        plan = plan_upgrade(root=root)
        self.assertEqual(plan["tip_commit"], tip)
        self.assertNotEqual(plan["tip_commit"], head)
        result = apply_upgrade(tip, root=root)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["commit"], tip)


class RestartDefaultData(unittest.TestCase):
    def test_omitted_data_resolves_default(self):
        with mock.patch("pangpang.install_git.sys") as fake_sys:
            fake_sys.executable = "/usr/bin/python3"
            fake_sys.argv = ["/usr/bin/python3", "-m", "pangpang"]
            with mock.patch("pangpang.install_git.is_source_checkout", return_value=True):
                with mock.patch("pangpang.cli.source_root", return_value=Path("/tmp/repo")):
                    argv = restart_argv("pangpang")
        self.assertEqual(argv[:4], ["/usr/bin/python3", "-m", "pangpang", "--data"])
        self.assertTrue(argv[4].endswith("data-v2"))
