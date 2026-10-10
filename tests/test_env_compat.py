import logging
import os

import pytest

from pangpang import env_compat
from pangpang.env_compat import env_value, has_env, legacy_name, with_legacy_names


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("PANGPANG_", "XIAN_PANGPANG_")):
            monkeypatch.delenv(name, raising=False)
    env_compat._warned.clear()
    yield
    env_compat._warned.clear()


def test_legacy_name_mapping():
    assert legacy_name("PANGPANG_TOKEN") == "XIAN_PANGPANG_TOKEN"
    assert legacy_name("TYPESAFE_API_KEY") is None


def test_legacy_only_reads_and_warns_without_leaking_value(monkeypatch, caplog):
    monkeypatch.setenv("XIAN_PANGPANG_TOKEN", "legacy-secret-value")
    with caplog.at_level(logging.WARNING, logger="pangpang.env"):
        assert env_value("PANGPANG_TOKEN", "") == "legacy-secret-value"
    assert "XIAN_PANGPANG_TOKEN" in caplog.text
    assert "PANGPANG_TOKEN" in caplog.text
    assert "legacy-secret-value" not in caplog.text


def test_new_name_wins_when_both_present(monkeypatch):
    monkeypatch.setenv("XIAN_PANGPANG_TOKEN", "old")
    monkeypatch.setenv("PANGPANG_TOKEN", "new")
    assert env_value("PANGPANG_TOKEN", "") == "new"


def test_present_but_invalid_new_name_never_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("XIAN_PANGPANG_TOKEN", "x" * 40)
    monkeypatch.setenv("PANGPANG_TOKEN", "short")
    assert env_value("PANGPANG_TOKEN", "") == "short"


def test_missing_returns_default():
    assert env_value("PANGPANG_TOKEN", "") == ""
    assert env_value("PANGPANG_PI_WORKER", None) is None


def test_warning_emitted_once_per_variable_per_process(monkeypatch, caplog):
    monkeypatch.setenv("XIAN_PANGPANG_LOG_LEVEL", "DEBUG")
    with caplog.at_level(logging.WARNING, logger="pangpang.env"):
        env_value("PANGPANG_LOG_LEVEL", "INFO")
        env_value("PANGPANG_LOG_LEVEL", "INFO")
    assert caplog.text.count("XIAN_PANGPANG_LOG_LEVEL") == 1


def test_has_env_covers_legacy_and_selector_conflict_counting(monkeypatch):
    assert not has_env("PANGPANG_PI_WORKER")
    monkeypatch.setenv("XIAN_PANGPANG_PI_WORKER", "/bin/true")
    assert has_env("PANGPANG_PI_WORKER")
    monkeypatch.setenv("PANGPANG_PI_NODE_BUNDLE", "/tmp/x")
    selectors = [name for name in env_compat.MIGRATED_ENV_NAMES
                 if name.endswith(("_WORKER", "_NODE_BUNDLE", "_BUN_BUNDLE")) and has_env(name)]
    assert len(selectors) == 2


def test_with_legacy_names_projection():
    projected = with_legacy_names({"PANGPANG_TOKEN", "TYPESAFE_API_KEY"})
    assert projected == {"PANGPANG_TOKEN", "XIAN_PANGPANG_TOKEN", "TYPESAFE_API_KEY"}


def test_secret_env_names_projection_covers_legacy(monkeypatch):
    from pangpang.semantic_gate import secret_env_names
    names = secret_env_names({"connector": {"credential_env": "PANGPANG_DEVICE_TOKEN"}})
    assert "XIAN_PANGPANG_TOKEN" in names
    assert "XIAN_PANGPANG_DEVICE_TOKEN" in names


def test_cli_resolve_access_token_accepts_legacy_name(monkeypatch, tmp_path):
    from pangpang import cli
    monkeypatch.setenv("XIAN_PANGPANG_TOKEN", "l" * 40)
    assert cli.resolve_access_token(tmp_path, False, cli._parser()) == "l" * 40


def test_cli_noninteractive_error_mentions_both_names(monkeypatch, tmp_path, capsys):
    from pangpang import cli
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    with pytest.raises(SystemExit):
        cli.resolve_access_token(tmp_path, False, cli._parser())
    err = capsys.readouterr().err
    assert "PANGPANG_TOKEN" in err
    assert "XIAN_PANGPANG_TOKEN" in err
