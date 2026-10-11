"""Regression tests for the install, release-identity, and backup baseline.

Covers: editable node bootstrap must not drop devDependencies or require a
repo-root .venv; an installed package's default data directory ignores the
working directory and will not silently replace a legacy relative tree; two
builds that share a version string are not the same rollback artifact; a
backup that is only the SQLite file is not a restore; certificate failures
stay distinct and do not disable verification.
"""
import argparse
import contextlib
import datetime
import io
import json
import os
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from pangpang import __version__
from pangpang.backup import BackupRefusal, create_consistent_backup, restore_consistent_backup
from pangpang.build_info import release_identity, same_rollback_artifact
from pangpang.cli import DataLocationError, dispatch, ensure_model_runtime, resolve_data_dir, source_root
from pangpang.connector import cloud_connection_fault
from pangpang.product_runtime import _check_manifest_identity
from pangpang.store import Store
from pangpang.vault import Vault


class EditableRuntimeTests(unittest.TestCase):
    def test_missing_venv_does_not_crash_or_omit_dev(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "package-lock.json").write_bytes(b'{"lockfileVersion": 3}\n')
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            (root / "node_modules").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0)

        with patch("pangpang.cli.subprocess.check_output", return_value="v22.19.0\n"), \
             patch("pangpang.cli.subprocess.run", side_effect=fake_run), \
             patch("pangpang.pi_worker_command.packaged_worker_command", return_value=None):
            ensure_model_runtime(root, root / ".venv", argparse.ArgumentParser(), omit_dev=False)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--omit=dev", calls[0])
        self.assertIn("ci", calls[0])
        self.assertFalse((root / ".venv").exists())
        self.assertTrue((root / "node_modules" / "pangpang-node-version").is_file())

    def test_launch_bootstrap_still_omits_dev_dependencies(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        venv = root / ".venv"
        venv.mkdir()
        (root / "package-lock.json").write_bytes(b"{}\n")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            (root / "node_modules").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0)

        with patch("pangpang.cli.subprocess.check_output", return_value="v22.19.0\n"), \
             patch("pangpang.cli.subprocess.run", side_effect=fake_run), \
             patch("pangpang.pi_worker_command.packaged_worker_command", return_value=None):
            ensure_model_runtime(root, venv, argparse.ArgumentParser())
        self.assertIn("--omit=dev", calls[0])
        self.assertTrue((venv / "pangpang-node-version").is_file())


class DataDirectoryTests(unittest.TestCase):
    def test_installed_default_ignores_working_directory(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        first = Path(tmp.name) / "one"
        second = Path(tmp.name) / "two"
        first.mkdir()
        second.mkdir()
        env = patch.dict(os.environ, {"XDG_DATA_HOME": str(Path(tmp.name) / "xdg")})
        env.start()
        self.addCleanup(env.stop)
        cwd = os.getcwd()
        try:
            os.chdir(first)
            left = resolve_data_dir("cloud", None, source_checkout=False)
            os.chdir(second)
            right = resolve_data_dir("cloud", None, source_checkout=False)
        finally:
            os.chdir(cwd)
        self.assertEqual(left, right)
        self.assertEqual(left, Path(tmp.name) / "xdg" / "pangpang")
        self.assertFalse(left.exists())

    def test_legacy_relative_tree_is_not_migrated_or_replaced(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cwd = os.getcwd()
        work = Path(tmp.name) / "work"
        work.mkdir()
        legacy = work / "data-v2"
        legacy.mkdir()
        (legacy / "bots.db").write_bytes(b"existing")
        (legacy / "vault").mkdir()
        stable_root = Path(tmp.name) / "xdg"
        env = patch.dict(os.environ, {"XDG_DATA_HOME": str(stable_root)})
        env.start()
        self.addCleanup(env.stop)
        try:
            os.chdir(work)
            with self.assertRaises(DataLocationError) as raised:
                resolve_data_dir("cloud", None, source_checkout=False)
        finally:
            os.chdir(cwd)
        self.assertIn(str(legacy.resolve()), str(raised.exception))
        self.assertFalse((stable_root / "pangpang").exists())
        self.assertTrue((legacy / "bots.db").is_file())

    def test_explicit_data_is_honored(self):
        chosen = Path("/tmp/pangpang-explicit-data")
        self.assertEqual(resolve_data_dir("cloud", str(chosen), source_checkout=False), chosen.absolute())

    def test_source_checkout_default_is_repo_local(self):
        self.assertEqual(resolve_data_dir("cloud", None, source_checkout=True),
                         (source_root() / "data-v2").absolute())
        self.assertEqual(resolve_data_dir("connector", None, source_checkout=True),
                         (source_root() / "connector-data").absolute())

    def test_installed_start_from_another_directory_announces_the_new_tree(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        work = Path(tmp.name) / "elsewhere"
        work.mkdir()
        xdg = Path(tmp.name) / "xdg"
        parser = argparse.ArgumentParser()
        args = argparse.Namespace(role="connector", data=None, show_access_key=False)
        cwd = os.getcwd()
        err = io.StringIO()
        try:
            os.chdir(work)
            with patch("pangpang.layout.is_source_checkout", return_value=False), \
                 patch("pangpang.connector.main", return_value=0), \
                 patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg)}), \
                 contextlib.redirect_stderr(err):
                dispatch(args, [], parser)
        finally:
            os.chdir(cwd)
        text = err.getvalue()
        self.assertIn(str((xdg / "pangpang-connector").absolute()), text)
        self.assertIn("--data", text)
        self.assertFalse((xdg / "pangpang-connector").exists())
        stable = xdg / "pangpang-connector"
        stable.mkdir(parents=True)
        (stable / "connector.db").write_bytes(b"x")
        err = io.StringIO()
        try:
            os.chdir(work)
            with patch("pangpang.layout.is_source_checkout", return_value=False), \
                 patch("pangpang.connector.main", return_value=0), \
                 patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg)}), \
                 contextlib.redirect_stderr(err):
                dispatch(args, [], parser)
        finally:
            os.chdir(cwd)
        self.assertEqual(err.getvalue(), "")


class ReleaseIdentityTests(unittest.TestCase):
    def test_same_version_different_commit_is_not_one_artifact(self):
        left = {"version": "0.9.0", "commit": "a" * 40, "ref": "v0.9.0"}
        right = {"version": "0.9.0", "commit": "b" * 40, "ref": "v0.9.0"}
        self.assertFalse(same_rollback_artifact(left, right))
        self.assertTrue(same_rollback_artifact(left, dict(left)))
        self.assertFalse(same_rollback_artifact(left, {"version": "0.9.0", "commit": "unknown"}))
        self.assertFalse(same_rollback_artifact({"version": __version__, "commit": "unknown"}, release_identity()))

    def test_manifest_source_commit_must_match_baked_build(self):
        base = {"pangpang_version": __version__, "platform": "Linux", "machine": "x86_64",
                "pi_version": _pi_version(), "bun_version": _bun_version()}
        _check_manifest_identity(dict(base), "Linux", "x86_64")
        with patch("pangpang.product_runtime.build_identity", return_value=("a" * 40, "v0.9.0", "T")):
            mismatched = dict(base, source_commit="b" * 40)
            with self.assertRaises(ValueError) as raised:
                _check_manifest_identity(mismatched, "Linux", "x86_64")
            self.assertEqual(str(raised.exception), "runtime_source_commit_mismatch")
            _check_manifest_identity(dict(base, source_commit="a" * 40), "Linux", "x86_64")
        with patch("pangpang.product_runtime.build_identity", return_value=("unknown", "unknown", "unknown")):
            with self.assertRaises(ValueError):
                _check_manifest_identity(dict(base, source_commit="c" * 40), "Linux", "x86_64")


def _pi_version():
    from pangpang.execution import PI_VERSION
    return PI_VERSION


def _bun_version():
    from pangpang.runtime_pins import BUN_VERSION
    return BUN_VERSION


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "cloud"
        self.data.mkdir()
        self.store = Store(self.data / "bots.db")
        self.store.put("settings", {"id": "managed", "config": {
            "hosts": [{"id": "local", "transport": "local"}],
            "semantic_gate": {"mode": "off"}}})
        self.store.put("identity", {"id": "device", "instance_id": "device-keep"})
        self.store.close()
        self.vault = Vault(self.data / "vault")
        self.vault.set("web-access-token", "t" * 32)
        self.vault.close()
        self.key_before = (self.data / "vault" / "master.key").read_bytes()

    def test_refuses_without_confirmation_and_writes_nothing(self):
        dest = Path(self.tmp.name) / "backup"
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(BackupRefusal) as raised:
                create_consistent_backup(self.data, dest, confirm=False, interactive=False)
        self.assertEqual(raised.exception.code, "confirmation_required")
        self.assertFalse(dest.exists())
        self.assertEqual((self.data / "vault" / "master.key").read_bytes(), self.key_before)

    def test_backup_includes_vault_and_restore_roundtrip(self):
        dest = Path(self.tmp.name) / "backup"
        result = create_consistent_backup(
            self.data, dest, confirm=True,
            now=datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc))
        self.assertTrue(result["upgrade_allowed"])
        manifest = json.loads((dest / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["sqlite_only_is_rollback"])
        self.assertFalse(manifest["version_is_rollback_identity"])
        names = {part["path"] for part in manifest["parts"]}
        self.assertEqual(names, {"bots.db", "vault/master.key", "vault/secrets.db"})
        self.assertIn("device-keep", manifest["inventory"]["device_instance_ids"])
        self.assertEqual(manifest["created_at"], "2026-01-02T03:04:05Z")
        self.assertEqual((self.data / "vault" / "master.key").read_bytes(), self.key_before)
        restored = Path(self.tmp.name) / "restored"
        restore_consistent_backup(dest, restored, confirm=True)
        again = Vault(restored / "vault")
        try:
            self.assertEqual(again.get("web-access-token"), "t" * 32)
        finally:
            again.close()
        restored_store = Store(restored / "bots.db")
        try:
            self.assertEqual(restored_store.get("identity", "device")["instance_id"], "device-keep")
        finally:
            restored_store.close()

    def test_cli_backup_reports_blocked_upgrade(self):
        store = Store(self.data / "bots.db")
        managed = store.get("settings", "managed")
        managed["config"]["semantic_gate"] = {"mode": "enforce"}
        store.put("settings", managed)
        store.close()
        from pangpang.cli import main
        dest = Path(self.tmp.name) / "cli-backup"
        err = __import__("io").StringIO()
        with __import__("contextlib").redirect_stderr(err):
            code = main(["backup", "--data", str(self.data), "--dest", str(dest), "--confirm"])
        self.assertEqual(code, 3)
        self.assertIn("semantic_gate_mode_not_off", err.getvalue())
        live = Store(self.data / "bots.db")
        try:
            self.assertEqual(live.get("settings", "managed")["config"]["semantic_gate"]["mode"], "enforce")
        finally:
            live.close()

    def test_unknown_task_blocks_upgrade_and_is_not_rewritten(self):
        store = Store(self.data / "bots.db")
        store.put("task", {"id": "task-open", "status": "unknown"})
        store.close()
        dest = Path(self.tmp.name) / "backup-blocked"
        result = create_consistent_backup(self.data, dest, confirm=True)
        self.assertFalse(result["upgrade_allowed"])
        self.assertIn("task_not_terminal", result["upgrade_blocked"])
        live = Store(self.data / "bots.db")
        try:
            self.assertEqual(live.get("task", "task-open")["status"], "unknown")
        finally:
            live.close()

    def test_ssh_host_is_inventoried_not_deleted(self):
        store = Store(self.data / "bots.db")
        managed = store.get("settings", "managed")
        managed["config"]["hosts"].append({"id": "old-ssh", "transport": "ssh"})
        store.put("settings", managed)
        store.close()
        dest = Path(self.tmp.name) / "backup-ssh"
        result = create_consistent_backup(self.data, dest, confirm=True)
        self.assertIn("ssh_host_present", result["upgrade_blocked"])
        live = Store(self.data / "bots.db")
        try:
            hosts = live.get("settings", "managed")["config"]["hosts"]
        finally:
            live.close()
        self.assertIn("old-ssh", [host["id"] for host in hosts])

    def test_sqlite_only_manifest_is_not_a_restore(self):
        dest = Path(self.tmp.name) / "sqlite-only"
        dest.mkdir()
        db = dest / "bots.db"
        _copy_db(self.data / "bots.db", db)
        (dest / "MANIFEST.json").write_text(json.dumps({
            "format": "pangpang-backup-v1",
            "role": "cloud",
            "sqlite_only_is_rollback": True,
            "required_parts": ["bots.db"],
            "parts": [{"path": "bots.db", "sha256": _sha(db)}],
        }), encoding="utf-8")
        target = Path(self.tmp.name) / "nope"
        with self.assertRaises(BackupRefusal) as raised:
            restore_consistent_backup(dest, target, confirm=True)
        self.assertEqual(raised.exception.code, "sqlite_only_not_rollback")
        self.assertFalse((target / "bots.db").exists())

    def test_checksum_mismatch_writes_nothing(self):
        dest = Path(self.tmp.name) / "good"
        create_consistent_backup(self.data, dest, confirm=True)
        key = dest / "vault" / "master.key"
        key.write_bytes(key.read_bytes() + b"x")
        target = Path(self.tmp.name) / "bad-restore"
        with self.assertRaises(BackupRefusal) as raised:
            restore_consistent_backup(dest, target, confirm=True)
        self.assertEqual(raised.exception.code, "checksum_mismatch")
        self.assertFalse(target.exists())

    def test_running_service_refuses_backup(self):
        lock_path = self.data / "coordinator.lock"
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; from pangpang.backup import _lock; "
             "handle = _lock(sys.argv[1]); "
             "print('locked', flush=True); sys.stdin.read()",
             str(lock_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        line = proc.stdout.readline()
        proc.stdout.close()
        self.assertEqual(line.strip(), "locked")
        dest = Path(self.tmp.name) / "while-running"
        with self.assertRaises(BackupRefusal) as raised:
            create_consistent_backup(self.data, dest, confirm=True)
        self.assertEqual(raised.exception.code, "service_running")
        self.assertFalse(dest.exists())
        proc.stdin.close()
        proc.wait(timeout=10)

    def test_insufficient_space_writes_nothing(self):
        import shutil
        dest = Path(self.tmp.name) / "nospace" / "backup"
        usage = shutil.disk_usage(self.tmp.name)
        with patch("pangpang.backup.shutil.disk_usage", return_value=usage._replace(free=0)):
            with self.assertRaises(BackupRefusal) as raised:
                create_consistent_backup(self.data, dest, confirm=True)
        self.assertEqual(raised.exception.code, "insufficient_space")
        self.assertFalse(dest.exists())
        self.assertEqual((self.data / "vault" / "master.key").read_bytes(), self.key_before)

    def test_copy_failure_leaves_no_backup_directory(self):
        dest = Path(self.tmp.name) / "partial"
        with patch("pangpang.backup._copy_sqlite", side_effect=OSError("no space")):
            with self.assertRaises(OSError):
                create_consistent_backup(self.data, dest, confirm=True)
        self.assertFalse(dest.exists())
        self.assertFalse(Path(str(dest) + ".partial").exists())
        self.assertEqual((self.data / "vault" / "master.key").read_bytes(), self.key_before)

    def test_startup_does_not_invoke_backup(self):
        text = (source_root() / "pangpang" / "__main__.py").read_text(encoding="utf-8")
        self.assertNotIn("create_consistent_backup", text)
        self.assertNotIn("restore_consistent_backup", text)

    def test_unsettled_connector_run_blocks_upgrade_and_is_not_rewritten(self):
        data = Path(self.tmp.name) / "connector"
        data.mkdir()
        store = Store(data / "connector.db")
        store.put("local_run", {"id": "run-1", "status": "launching"})
        store.put("identity", {"id": "device", "instance_id": "dev-1"})
        store.close()
        vault = Vault(data / "vault")
        vault.set("web-access-token", "c" * 32)
        vault.close()
        for status in ("launching", "active", "report"):
            store = Store(data / "connector.db")
            row = store.get("local_run", "run-1")
            row["status"] = status
            store.put("local_run", row)
            store.close()
            dest = Path(self.tmp.name) / ("run-" + status)
            result = create_consistent_backup(data, dest, confirm=True)
            self.assertIn("local_run_not_terminal", result["upgrade_blocked"])
            self.assertFalse(result["upgrade_allowed"])
            live = Store(data / "connector.db")
            try:
                self.assertEqual(live.get("local_run", "run-1")["status"], status)
            finally:
                live.close()
        blocked_rows = (
            {"status": "acknowledged"},
            {"status": "acknowledged", "task": {"id": "task-1"}, "receipt": {
                "status": "running", "process_tree_confirmed": False}},
        )
        for index, row in enumerate(blocked_rows):
            store = Store(data / "connector.db")
            current = store.get("local_run", "run-1")
            current.clear()
            current.update({"id": "run-1"}, **row)
            store.put("local_run", current)
            store.close()
            dest = Path(self.tmp.name) / ("run-open-%s" % index)
            result = create_consistent_backup(data, dest, confirm=True)
            self.assertIn("local_run_not_terminal", result["upgrade_blocked"])
            self.assertFalse(result["upgrade_allowed"])
            live = Store(data / "connector.db")
            try:
                self.assertEqual(live.get("local_run", "run-1")["status"], "acknowledged")
            finally:
                live.close()
        store = Store(data / "connector.db")
        current = store.get("local_run", "run-1")
        current.clear()
        current.update({
            "id": "run-1",
            "status": "acknowledged",
            "task": {},
            "receipt": {"status": "cancelled", "process_tree_confirmed": True},
        })
        store.put("local_run", current)
        store.close()
        dest = Path(self.tmp.name) / "run-settled"
        result = create_consistent_backup(data, dest, confirm=True)
        self.assertNotIn("local_run_not_terminal", result["upgrade_blocked"])
        self.assertTrue(result["upgrade_allowed"])

    def test_restore_copy_failure_keeps_existing_data(self):
        dest = Path(self.tmp.name) / "backup-copy-fail"
        create_consistent_backup(self.data, dest, confirm=True)
        store = Store(self.data / "bots.db")
        store.put("identity", {"id": "device", "instance_id": "changed"})
        store.close()
        real = shutil.copy2

        def boom(src, dst, *args, **kwargs):
            if str(dst).endswith("secrets.db"):
                raise OSError("disk full")
            return real(src, dst, *args, **kwargs)

        with patch("pangpang.backup.shutil.copy2", side_effect=boom):
            with self.assertRaises(BackupRefusal) as raised:
                restore_consistent_backup(dest, self.data, confirm=True)
        self.assertEqual(raised.exception.code, "restore_failed")
        self.assertFalse((self.data.parent / (self.data.name + ".restore-partial")).exists())
        self.assertFalse((self.data.parent / (self.data.name + ".restore-aside")).exists())
        live = Store(self.data / "bots.db")
        try:
            self.assertEqual(live.get("identity", "device")["instance_id"], "changed")
        finally:
            live.close()
        self.assertEqual((self.data / "vault" / "master.key").read_bytes(), self.key_before)
        vault = Vault(self.data / "vault")
        try:
            self.assertEqual(vault.get("web-access-token"), "t" * 32)
        finally:
            vault.close()

    def test_restore_rename_failure_rolls_back_existing_data(self):
        dest = Path(self.tmp.name) / "backup-rename-fail"
        create_consistent_backup(self.data, dest, confirm=True)
        store = Store(self.data / "bots.db")
        store.put("identity", {"id": "device", "instance_id": "changed"})
        store.close()
        real = os.rename

        def flaky(src, dst):
            parent = Path(src).parent.name
            if Path(src).name == "vault" and parent.endswith(".restore-partial"):
                raise OSError("rename failed")
            return real(src, dst)

        with patch("pangpang.backup.os.rename", side_effect=flaky):
            with self.assertRaises(BackupRefusal) as raised:
                restore_consistent_backup(dest, self.data, confirm=True)
        self.assertEqual(raised.exception.code, "restore_failed")
        self.assertFalse((self.data.parent / (self.data.name + ".restore-partial")).exists())
        self.assertFalse((self.data.parent / (self.data.name + ".restore-aside")).exists())
        live = Store(self.data / "bots.db")
        try:
            self.assertEqual(live.get("identity", "device")["instance_id"], "changed")
        finally:
            live.close()
        self.assertEqual((self.data / "vault" / "master.key").read_bytes(), self.key_before)
        vault = Vault(self.data / "vault")
        try:
            self.assertEqual(vault.get("web-access-token"), "t" * 32)
        finally:
            vault.close()

    def test_restore_rollback_failure_names_the_mixed_directory(self):
        dest = Path(self.tmp.name) / "backup-rollback-fail"
        create_consistent_backup(self.data, dest, confirm=True)
        store = Store(self.data / "bots.db")
        store.put("identity", {"id": "device", "instance_id": "changed"})
        store.close()
        real = os.rename

        def flaky(src, dst):
            parent = Path(src).parent.name
            if Path(src).name == "vault" and (parent.endswith(".restore-partial") or parent.endswith(".restore-aside")):
                raise OSError("rename failed")
            return real(src, dst)

        with patch("pangpang.backup.os.rename", side_effect=flaky):
            with self.assertRaises(BackupRefusal) as raised:
                restore_consistent_backup(dest, self.data, confirm=True)
        aside = self.data.parent / (self.data.name + ".restore-aside")
        self.assertEqual(raised.exception.code, "restore_incomplete")
        self.assertIn(str(aside), str(raised.exception))
        self.assertNotIn("保持原样", str(raised.exception))
        self.assertTrue((aside / "vault" / "master.key").is_file())
        self.assertFalse((self.data / "vault" / "master.key").exists())
        self.assertEqual((aside / "vault" / "master.key").read_bytes(), self.key_before)

    def test_restore_aside_cleanup_failure_is_not_success(self):
        dest = Path(self.tmp.name) / "backup-cleanup-fail"
        create_consistent_backup(self.data, dest, confirm=True)
        store = Store(self.data / "bots.db")
        store.put("identity", {"id": "device", "instance_id": "changed"})
        store.close()
        real = shutil.rmtree

        def boom(path, *args, **kwargs):
            if Path(path).name.endswith(".restore-aside"):
                raise OSError("busy")
            return real(path, *args, **kwargs)

        with patch("pangpang.backup.shutil.rmtree", side_effect=boom):
            with self.assertRaises(BackupRefusal) as raised:
                restore_consistent_backup(dest, self.data, confirm=True)
        aside = self.data.parent / (self.data.name + ".restore-aside")
        self.assertEqual(raised.exception.code, "restore_cleanup_failed")
        self.assertIn(str(aside), str(raised.exception))
        self.assertTrue(aside.is_dir())
        self.assertTrue((aside / "vault" / "master.key").is_file())
        live = Store(self.data / "bots.db")
        try:
            self.assertEqual(live.get("identity", "device")["instance_id"], "device-keep")
        finally:
            live.close()

    def test_restore_replaces_existing_directory(self):
        dest = Path(self.tmp.name) / "backup-replace"
        create_consistent_backup(self.data, dest, confirm=True)
        store = Store(self.data / "bots.db")
        store.put("identity", {"id": "device", "instance_id": "changed"})
        store.close()
        restore_consistent_backup(dest, self.data, confirm=True)
        live = Store(self.data / "bots.db")
        try:
            self.assertEqual(live.get("identity", "device")["instance_id"], "device-keep")
        finally:
            live.close()
        vault = Vault(self.data / "vault")
        try:
            self.assertEqual(vault.get("web-access-token"), "t" * 32)
        finally:
            vault.close()

    def test_manifest_rejects_paths_outside_the_backup(self):
        dest = Path(self.tmp.name) / "backup-escape"
        create_consistent_backup(self.data, dest, confirm=True)
        manifest = json.loads((dest / "MANIFEST.json").read_text(encoding="utf-8"))
        manifest["required_parts"] = ["../outside.db", "vault/master.key", "vault/secrets.db"]
        manifest["parts"].append({"path": "../outside.db", "sha256": "ab" * 32, "bytes": 1})
        (dest / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
        outside = Path(self.tmp.name) / "outside.db"
        outside.write_bytes(b"nope")
        target = Path(self.tmp.name) / "escaped"
        with self.assertRaises(BackupRefusal) as raised:
            restore_consistent_backup(dest, target, confirm=True)
        self.assertEqual(raised.exception.code, "manifest_invalid")
        self.assertFalse(target.exists())
        self.assertEqual(outside.read_bytes(), b"nope")

    def test_manifest_rejects_symlink_part(self):
        dest = Path(self.tmp.name) / "backup-link"
        create_consistent_backup(self.data, dest, confirm=True)
        key = dest / "vault" / "master.key"
        payload = key.read_bytes()
        key.unlink()
        outside = Path(self.tmp.name) / "linked-key"
        outside.write_bytes(payload)
        key.symlink_to(outside)
        target = Path(self.tmp.name) / "linked-restore"
        with self.assertRaises(BackupRefusal) as raised:
            restore_consistent_backup(dest, target, confirm=True)
        self.assertEqual(raised.exception.code, "manifest_invalid")
        self.assertFalse(target.exists())
        self.assertEqual(outside.read_bytes(), payload)

    def test_windows_backup_directory_uses_account_acl(self):
        seen = []

        def fake(path):
            seen.append(str(path))

        dest = Path(self.tmp.name) / "win-backup"
        restored = Path(self.tmp.name) / "win-restored"
        with patch("pangpang.backup._windows_acl", return_value=True), \
             patch("pangpang.vault._secure_windows_directory", fake):
            create_consistent_backup(self.data, dest, confirm=True)
            restore_consistent_backup(dest, restored, confirm=True)
        self.assertTrue(any(path.endswith("win-backup.partial") for path in seen))
        self.assertTrue(any(path.endswith(os.path.join("win-backup.partial", "vault")) for path in seen))
        self.assertTrue(any(path.endswith("win-restored") for path in seen))
        vault = Vault(restored / "vault")
        try:
            self.assertEqual(vault.get("web-access-token"), "t" * 32)
        finally:
            vault.close()


class ConnectorTlsTests(unittest.TestCase):
    def test_certificate_failure_is_distinct_and_does_not_skip_verification(self):
        fault = cloud_connection_fault(urllib.error.URLError(
            ssl.SSLError("certificate verify failed: unable to get local issuer certificate")))
        self.assertEqual(fault.code, "connector_tls_untrusted")
        self.assertIn("不要关闭", fault.advice)
        hostname = cloud_connection_fault(urllib.error.URLError(
            ssl.SSLError("certificate verify failed: Hostname mismatch")))
        self.assertEqual(hostname.code, "connector_tls_hostname_mismatch")
        refused = cloud_connection_fault(urllib.error.URLError(ConnectionRefusedError()))
        self.assertEqual(refused.code, "cloud_connection_failed")
        import inspect
        from pangpang.connector import Connector
        source = inspect.getsource(Connector.request)
        self.assertNotIn("CERT_NONE", source)
        self.assertNotIn("_create_unverified_context", source)
        self.assertNotIn("check_hostname", source)


class DocGateContractTests(unittest.TestCase):
    def test_unittest_discovery_does_not_import_pytest(self):
        import ast
        root = source_root() / "tests"
        offenders = []
        for path in root.glob("test_*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                if any(name == "pytest" or name.startswith("pytest.") for name in names):
                    offenders.append(path.name)
                    break
        self.assertEqual(offenders, [])

    def test_published_docs_do_not_use_a_lan_ip_as_https(self):
        root = source_root()
        for relative in ("README.md", "README.zh.md", "docs/INSTALL.md", "docs/RUNBOOK.md", "docs/HTTPS.md"):
            text = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("https://192.168.", text, relative)
            self.assertNotIn("http://192.168.", text, relative)

    def test_security_statement_matches_defaults(self):
        text = (source_root() / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("grant_write", text)
        self.assertIn("web_search", text)
        self.assertIn("unknown", text)

    def test_systemd_units_name_a_non_root_account(self):
        root = source_root() / "deploy"
        for name in ("pangpang-cloud.service", "pangpang-connector.service"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn("\nUser=pangpang\n", text)
            self.assertIn("\nGroup=pangpang\n", text)
            self.assertNotIn("User=root", text)
            self.assertNotIn("Group=root", text)


def _sha(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_db(source, dest):
    src = sqlite3.connect(source)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


if __name__ == "__main__":
    unittest.main()
