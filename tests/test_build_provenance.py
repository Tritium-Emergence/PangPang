import contextlib
import datetime
import importlib.util
import io
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pangpang import __version__
from pangpang.build_info import build_identity, build_line, version_string

_spec = importlib.util.spec_from_file_location(
    "build_provenance",
    Path(__file__).resolve().parents[1] / "tools" / "build_provenance.py")
build_provenance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_provenance)


def _init_repo(path):
    def run(*args):
        return subprocess.run(["git", *args], cwd=str(path), capture_output=True,
                              text=True, check=True)
    (path / "tracked.txt").write_text("x\n", encoding="utf-8")
    run("init", "-q")
    run("config", "user.email", "t@t.local")
    run("config", "user.name", "t")
    run("add", ".")
    run("commit", "-qm", "base")
    return run


class BuildIdentityTests(unittest.TestCase):
    def test_build_identity_reads_generated_module(self):
        module = types.ModuleType("pangpang._build")
        module.BUILD_COMMIT = "a" * 40
        module.BUILD_REF = "v0.9.0-8-g" + "a" * 7
        module.BUILT_AT = "2026-10-10T00:00:00Z"
        module.__spec__ = None
        with patch.dict(sys.modules, {"pangpang._build": module}):
            commit, ref, built_at = build_identity()
            self.assertEqual(commit, "a" * 40)
            self.assertEqual(ref, "v0.9.0-8-g" + "a" * 7)
            self.assertEqual(built_at, "2026-10-10T00:00:00Z")
            self.assertTrue(version_string().startswith(
                __version__ + " (build aaaaaaa @ v0.9.0-8-g" + "a" * 7 + ", built 2026-10-10T00:00:00Z)"))
            self.assertEqual(build_line(), "build: %s ref=v0.9.0-8-g%s (built 2026-10-10T00:00:00Z)\n" % (
                "a" * 40, "a" * 7))

    def test_build_identity_tolerates_pre_ref_artifact(self):
        module = types.ModuleType("pangpang._build")
        module.BUILD_COMMIT = "d" * 40
        module.BUILT_AT = "T"
        module.__spec__ = None
        with patch.dict(sys.modules, {"pangpang._build": module}):
            self.assertEqual(build_identity(), ("d" * 40, "unknown", "T"))
            self.assertTrue(version_string().startswith(__version__ + " (build ddddddd, built T)"))

    def test_build_identity_unknown_without_module(self):
        with patch.dict(sys.modules, {"pangpang._build": None}):
            self.assertEqual(build_identity(), ("unknown", "unknown", "unknown"))
            self.assertEqual(version_string(), __version__ + " (build unknown)")
            self.assertEqual(build_line(), "build: unknown (built unknown)\n")

    def test_version_string_keeps_release_version_pure(self):
        module = types.ModuleType("pangpang._build")
        module.BUILD_COMMIT = "b" * 40
        module.BUILD_REF = "v0.9.0"
        module.BUILT_AT = "T"
        with patch.dict(sys.modules, {"pangpang._build": module}):
            self.assertTrue(version_string().startswith(__version__ + " ("))
        self.assertEqual(__version__, "0.9.1.dev0")


class CollectProvenanceTests(unittest.TestCase):
    def test_collect_provenance_fail_closed_when_required(self):
        def boom(*args, **kwargs):
            raise OSError("no git")
        with patch.object(build_provenance.subprocess, "run", boom):
            with self.assertRaises(SystemExit):
                build_provenance.collect_provenance(require=True)
            info = build_provenance.collect_provenance(require=False)
        self.assertEqual(info["BUILD_COMMIT"], "unknown")
        self.assertEqual(info["BUILD_REF"], "unknown")

    def test_collect_provenance_refuses_dirty_tree_when_required(self):
        def fake_run(cmd, *args, **kwargs):
            return types.SimpleNamespace(stdout="ref-1-g" + "e" * 7 + "-dirty\n")
        with patch.object(build_provenance.subprocess, "run", fake_run):
            with self.assertRaises(SystemExit):
                build_provenance.collect_provenance(require=True)
            info = build_provenance.collect_provenance(require=False)
        self.assertTrue(info["BUILD_REF"].endswith("-dirty"))

    def test_strict_mode_rejects_untracked_sources(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        _init_repo(path)
        (path / "newly_added.py").write_text("evil\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            build_provenance.collect_provenance(require=True, root=str(path))
        self.assertEqual(len(build_provenance.collect_provenance(
            require=False, root=str(path))["BUILD_COMMIT"]), 40)

    def test_strict_mode_rejects_modified_tracked_tree(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        _init_repo(path)
        (path / "tracked.txt").write_text("modified\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            build_provenance.collect_provenance(require=True, root=str(path))

    def test_strict_mode_accepts_clean_repo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        _init_repo(path)
        info = build_provenance.collect_provenance(require=True, root=str(path))
        self.assertEqual(len(info["BUILD_COMMIT"]), 40)

    def test_git_queries_pinned_to_source_root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = root / "source"
        source.mkdir()
        _init_repo(source)
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        cwd = os_getcwd = __import__("os").getcwd()
        try:
            __import__("os").chdir(elsewhere)
            info = build_provenance.collect_provenance(require=False, root=str(source))
        finally:
            __import__("os").chdir(cwd)
        self.assertEqual(len(info["BUILD_COMMIT"]), 40)
        self.assertNotEqual(info["BUILD_REF"], "unknown")
        del os_getcwd

    def test_strict_mode_rejects_parent_repository_root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        parent = Path(tmp.name) / "parent"
        parent.mkdir()
        run = _init_repo(parent)
        (parent / ".gitignore").write_text("vendor/\n", encoding="utf-8")
        run("add", ".gitignore")
        run("commit", "-qm", "ignore vendor")
        vendor = parent / "vendor" / "pp"
        vendor.mkdir(parents=True)
        (vendor / "extra.py").write_text("x\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            build_provenance.collect_provenance(require=True, root=str(vendor))

    def test_strict_mode_rejects_git_status_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        _init_repo(path)
        (path / ".git" / "index").write_bytes(b"corrupted-index-payload")
        with self.assertRaises(SystemExit):
            build_provenance.collect_provenance(require=True, root=str(path))
        self.assertEqual(len(build_provenance.collect_provenance(
            require=False, root=str(path))["BUILD_COMMIT"]), 40)

    def test_collect_provenance_reads_git_head(self):
        info = build_provenance.collect_provenance(require=False)
        self.assertTrue(len(info["BUILD_COMMIT"]) == 40 or info["BUILD_COMMIT"] == "unknown")
        self.assertNotEqual(info["BUILD_REF"], "")
        self.assertTrue(info["BUILT_AT"].endswith("Z"))

    def test_built_at_uses_injected_utc_clock(self):
        frozen = datetime.datetime(2026, 10, 11, 1, 2, 3, tzinfo=datetime.timezone.utc)
        info = build_provenance.collect_provenance(require=False, now=frozen)
        again = build_provenance.collect_provenance(require=False, now=frozen)
        self.assertEqual(info["BUILT_AT"], "2026-10-11T01:02:03Z")
        self.assertEqual(again["BUILT_AT"], info["BUILT_AT"])

    def test_render_and_write_roundtrip(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = build_provenance.write_to(tmp.name,
                                         {"BUILD_COMMIT": "c" * 40, "BUILD_REF": "v1", "BUILT_AT": "T"})
        namespace = {}
        exec(Path(path).read_text(encoding="utf-8"), namespace)
        self.assertEqual(namespace["BUILD_COMMIT"], "c" * 40)
        self.assertEqual(namespace["BUILD_REF"], "v1")
        self.assertEqual(namespace["BUILT_AT"], "T")
        legacy = build_provenance.render({"BUILD_COMMIT": "c" * 40, "BUILT_AT": "T"})
        self.assertIn("BUILD_REF = 'unknown'", legacy)


class VersionFlagTests(unittest.TestCase):
    def test_cli_version_flag_exits_zero(self):
        from pangpang import cli
        out = io.StringIO()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(out):
            cli._parser().parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)
        text = out.getvalue()
        self.assertIn(__version__, text)
        self.assertIn("build", text)

    def test_cli_console_script_version_subprocess(self):
        result = subprocess.run([sys.executable, "-m", "pangpang.cli", "--version"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn(__version__, result.stdout)

    def test_module_entry_version_subprocess(self):
        result = subprocess.run([sys.executable, "-m", "pangpang", "--version"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn(__version__, result.stdout)


if __name__ == "__main__":
    unittest.main()
