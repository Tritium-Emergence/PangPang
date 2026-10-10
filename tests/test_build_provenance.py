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
    module.BUILT_AT = "2026-10-10T00:00:00Z"
    module.__spec__ = None
    monkeypatch.setitem(sys.modules, "pangpang._build", module)
    commit, built_at = build_identity()
    assert commit == "a" * 40
    assert built_at == "2026-10-10T00:00:00Z"
    assert version_string().startswith("0.9.0 (build aaaaaaa, built 2026-10-10T00:00:00Z)")
    assert build_line() == "build: %s (built 2026-10-10T00:00:00Z)\n" % ("a" * 40)


def test_build_identity_unknown_without_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "pangpang._build", None)
    assert build_identity() == ("unknown", "unknown")
    assert version_string() == "0.9.0 (build unknown)"
    assert build_line() == "build: unknown (built unknown)\n"


def test_version_string_keeps_release_version_pure(monkeypatch):
    module = types.ModuleType("pangpang._build")
    module.BUILD_COMMIT = "b" * 40
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
    assert build_provenance.collect_provenance(require=False)["BUILD_COMMIT"] == "unknown"


def test_collect_provenance_reads_git_head():
    info = build_provenance.collect_provenance(require=False)
    assert len(info["BUILD_COMMIT"]) == 40 or info["BUILD_COMMIT"] == "unknown"
    assert info["BUILT_AT"].endswith("Z")


def test_render_and_write_roundtrip(tmp_path):
    path = build_provenance.write_to(str(tmp_path),
                                     {"BUILD_COMMIT": "c" * 40, "BUILT_AT": "T"})
    namespace = {}
    exec(Path(path).read_text(encoding="utf-8"), namespace)
    assert namespace["BUILD_COMMIT"] == "c" * 40
    assert namespace["BUILT_AT"] == "T"


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
