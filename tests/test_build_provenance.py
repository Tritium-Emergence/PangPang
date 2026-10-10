import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

from pangpang import build_info
from pangpang.build_info import build_identity, build_line, version_string

_spec = importlib.util.spec_from_file_location(
    "build_provenance",
    Path(__file__).resolve().parents[1] / "tools" / "build_provenance.py")
build_provenance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_provenance)


def test_build_identity_reads_generated_module(monkeypatch):
    module = types.ModuleType("pangpang._build")
    module.BUILD_COMMIT = "a" * 40
    module.BUILD_REF = "v0.9.0-8-g" + "a" * 7
    module.BUILT_AT = "2026-10-10T00:00:00Z"
    module.__spec__ = None
    monkeypatch.setitem(sys.modules, "pangpang._build", module)
    commit, ref, built_at = build_identity()
    assert commit == "a" * 40
    assert ref == "v0.9.0-8-g" + "a" * 7
    assert built_at == "2026-10-10T00:00:00Z"
    assert version_string().startswith(
        "0.9.0 (build aaaaaaa @ v0.9.0-8-g" + "a" * 7 + ", built 2026-10-10T00:00:00Z)")
    assert build_line() == "build: %s ref=v0.9.0-8-g%s (built 2026-10-10T00:00:00Z)\n" % (
        "a" * 40, "a" * 7)


def test_build_identity_tolerates_pre_ref_artifact(monkeypatch):
    module = types.ModuleType("pangpang._build")
    module.BUILD_COMMIT = "d" * 40
    module.BUILT_AT = "T"
    module.__spec__ = None
    monkeypatch.setitem(sys.modules, "pangpang._build", module)
    assert build_identity() == ("d" * 40, "unknown", "T")
    assert version_string().startswith("0.9.0 (build ddddddd, built T)")


def test_build_identity_unknown_without_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "pangpang._build", None)
    assert build_identity() == ("unknown", "unknown", "unknown")
    assert version_string() == "0.9.0 (build unknown)"
    assert build_line() == "build: unknown (built unknown)\n"


def test_version_string_keeps_release_version_pure(monkeypatch):
    module = types.ModuleType("pangpang._build")
    module.BUILD_COMMIT = "b" * 40
    module.BUILD_REF = "v0.9.0"
    module.BUILT_AT = "T"
    monkeypatch.setitem(sys.modules, "pangpang._build", module)
    from pangpang import __version__
    assert version_string().startswith(__version__ + " (")
    assert __version__ == "0.9.0"


def test_collect_provenance_fail_closed_when_required(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no git")
    monkeypatch.setattr(build_provenance.subprocess, "run", boom)
    with pytest.raises(SystemExit):
        build_provenance.collect_provenance(require=True)
    info = build_provenance.collect_provenance(require=False)
    assert info["BUILD_COMMIT"] == "unknown"
    assert info["BUILD_REF"] == "unknown"


def test_collect_provenance_refuses_dirty_tree_when_required(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        return types.SimpleNamespace(stdout="ref-1-g" + "e" * 7 + "-dirty\n")
    monkeypatch.setattr(build_provenance.subprocess, "run", fake_run)
    with pytest.raises(SystemExit):
        build_provenance.collect_provenance(require=True)
    info = build_provenance.collect_provenance(require=False)
    assert info["BUILD_REF"].endswith("-dirty")


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


def test_strict_mode_rejects_untracked_sources(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "newly_added.py").write_text("evil\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        build_provenance.collect_provenance(require=True, root=str(tmp_path))
    assert len(build_provenance.collect_provenance(
        require=False, root=str(tmp_path))["BUILD_COMMIT"]) == 40


def test_strict_mode_rejects_modified_tracked_tree(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("modified\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        build_provenance.collect_provenance(require=True, root=str(tmp_path))


def test_strict_mode_accepts_clean_repo(tmp_path):
    _init_repo(tmp_path)
    info = build_provenance.collect_provenance(require=True, root=str(tmp_path))
    assert len(info["BUILD_COMMIT"]) == 40


def test_git_queries_pinned_to_source_root(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _init_repo(source)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    info = build_provenance.collect_provenance(require=False, root=str(source))
    assert len(info["BUILD_COMMIT"]) == 40
    assert info["BUILD_REF"] != "unknown"


def test_strict_mode_rejects_parent_repository_root(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    run = _init_repo(parent)
    (parent / ".gitignore").write_text("vendor/\n", encoding="utf-8")
    run("add", ".gitignore")
    run("commit", "-qm", "ignore vendor")
    vendor = parent / "vendor" / "pp"
    vendor.mkdir(parents=True)
    (vendor / "extra.py").write_text("x\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        build_provenance.collect_provenance(require=True, root=str(vendor))


def test_strict_mode_rejects_git_status_error(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / ".git" / "index").write_bytes(b"corrupted-index-payload")
    with pytest.raises(SystemExit):
        build_provenance.collect_provenance(require=True, root=str(tmp_path))
    assert len(build_provenance.collect_provenance(
        require=False, root=str(tmp_path))["BUILD_COMMIT"]) == 40


def test_collect_provenance_reads_git_head():
    info = build_provenance.collect_provenance(require=False)
    assert len(info["BUILD_COMMIT"]) == 40 or info["BUILD_COMMIT"] == "unknown"
    assert info["BUILD_REF"] != ""
    assert info["BUILT_AT"].endswith("Z")


def test_render_and_write_roundtrip(tmp_path):
    path = build_provenance.write_to(str(tmp_path),
                                     {"BUILD_COMMIT": "c" * 40, "BUILD_REF": "v1", "BUILT_AT": "T"})
    namespace = {}
    exec(Path(path).read_text(encoding="utf-8"), namespace)
    assert namespace["BUILD_COMMIT"] == "c" * 40
    assert namespace["BUILD_REF"] == "v1"
    assert namespace["BUILT_AT"] == "T"
    legacy = build_provenance.render({"BUILD_COMMIT": "c" * 40, "BUILT_AT": "T"})
    assert "BUILD_REF = 'unknown'" in legacy


def test_cli_version_flag_exits_zero(capsys):
    from pangpang import cli
    with pytest.raises(SystemExit) as raised:
        cli._parser().parse_args(["--version"])
    assert raised.value.code == 0
    out = capsys.readouterr().out
    assert "0.9.0" in out and "build" in out


def test_cli_console_script_version_subprocess():
    result = subprocess.run([sys.executable, "-m", "pangpang.cli", "--version"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "0.9.0" in result.stdout


def test_module_entry_version_subprocess():
    result = subprocess.run([sys.executable, "-m", "pangpang", "--version"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "0.9.0" in result.stdout
