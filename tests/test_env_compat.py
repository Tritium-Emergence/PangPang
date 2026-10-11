import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pangpang import env_compat
from pangpang.env_compat import env_value, has_env, legacy_name, with_legacy_names


class EnvCompatTests(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ[key] for key in os.environ
                       if key.startswith(("PANGPANG_", "XIAN_PANGPANG_"))}
        for key in self._saved:
            os.environ.pop(key, None)
        env_compat._warned.clear()

    def tearDown(self):
        for key in list(os.environ):
            if key.startswith(("PANGPANG_", "XIAN_PANGPANG_")):
                os.environ.pop(key, None)
        os.environ.update(self._saved)
        env_compat._warned.clear()

    def test_legacy_name_mapping(self):
        self.assertEqual(legacy_name("PANGPANG_TOKEN"), "XIAN_PANGPANG_TOKEN")
        self.assertIsNone(legacy_name("TYPESAFE_API_KEY"))

    def test_legacy_map_is_finite_and_covers_only_verified_variables(self):
        self.assertEqual(set(env_compat.LEGACY_ENV_NAMES), {
            "PANGPANG_TOKEN", "PANGPANG_DEVICE_TOKEN", "PANGPANG_PI_WORKER",
            "PANGPANG_LOG_LEVEL", "PANGPANG_SEMANTIC_GATE_URL"})
        self.assertIsNone(legacy_name("PANGPANG_PI_NODE_BUNDLE"))
        self.assertIsNone(legacy_name("PANGPANG_PI_BUN_BUNDLE"))
        self.assertIsNone(legacy_name("PANGPANG_SEARCH_PROVIDER"))

    def test_unverified_variable_never_reads_legacy_name(self):
        os.environ["XIAN_PANGPANG_PI_NODE_BUNDLE"] = "/tmp/stale-node"
        os.environ["XIAN_PANGPANG_PI_BUN_BUNDLE"] = "/tmp/stale-bun"
        os.environ["XIAN_PANGPANG_SEARCH_PROVIDER"] = "bing"
        self.assertIsNone(env_value("PANGPANG_PI_NODE_BUNDLE", None))
        self.assertIsNone(env_value("PANGPANG_PI_BUN_BUNDLE", None))
        self.assertEqual(env_value("PANGPANG_SEARCH_PROVIDER", ""), "")
        self.assertFalse(has_env("PANGPANG_PI_NODE_BUNDLE"))
        self.assertFalse(has_env("PANGPANG_PI_BUN_BUNDLE"))
        self.assertFalse(has_env("PANGPANG_SEARCH_PROVIDER"))

    def test_legacy_only_reads_and_warns_without_leaking_value(self):
        os.environ["XIAN_PANGPANG_TOKEN"] = "legacy-secret-value"
        with self.assertLogs("pangpang.env", level="WARNING") as captured:
            self.assertEqual(env_value("PANGPANG_TOKEN", ""), "legacy-secret-value")
        text = "\n".join(captured.output)
        self.assertIn("XIAN_PANGPANG_TOKEN", text)
        self.assertIn("PANGPANG_TOKEN", text)
        self.assertNotIn("legacy-secret-value", text)

    def test_new_name_wins_when_both_present(self):
        os.environ["XIAN_PANGPANG_TOKEN"] = "old"
        os.environ["PANGPANG_TOKEN"] = "new"
        self.assertEqual(env_value("PANGPANG_TOKEN", ""), "new")

    def test_present_but_invalid_new_name_never_falls_back_to_legacy(self):
        os.environ["XIAN_PANGPANG_TOKEN"] = "x" * 40
        os.environ["PANGPANG_TOKEN"] = "short"
        self.assertEqual(env_value("PANGPANG_TOKEN", ""), "short")

    def test_missing_returns_default(self):
        self.assertEqual(env_value("PANGPANG_TOKEN", ""), "")
        self.assertIsNone(env_value("PANGPANG_PI_WORKER", None))

    def test_warning_emitted_once_per_variable_per_process(self):
        os.environ["XIAN_PANGPANG_LOG_LEVEL"] = "DEBUG"
        with self.assertLogs("pangpang.env", level="WARNING") as captured:
            env_value("PANGPANG_LOG_LEVEL", "INFO")
            env_value("PANGPANG_LOG_LEVEL", "INFO")
        text = "\n".join(captured.output)
        self.assertEqual(text.count("XIAN_PANGPANG_LOG_LEVEL"), 1)

    def test_has_env_covers_legacy_and_selector_conflict_counting(self):
        from pangpang.pi_worker_command import _SELECTORS
        self.assertFalse(has_env("PANGPANG_PI_WORKER"))
        os.environ["XIAN_PANGPANG_PI_WORKER"] = "/bin/true"
        self.assertTrue(has_env("PANGPANG_PI_WORKER"))
        os.environ["PANGPANG_PI_NODE_BUNDLE"] = "/tmp/x"
        selectors = [name for name in _SELECTORS if has_env(name)]
        self.assertEqual(len(selectors), 2)
        os.environ.pop("PANGPANG_PI_NODE_BUNDLE")
        os.environ["XIAN_PANGPANG_PI_NODE_BUNDLE"] = "/tmp/x"
        self.assertEqual(len([name for name in _SELECTORS if has_env(name)]), 1)

    def test_with_legacy_names_projection(self):
        projected = with_legacy_names({"PANGPANG_TOKEN", "TYPESAFE_API_KEY"})
        self.assertEqual(projected, {"PANGPANG_TOKEN", "XIAN_PANGPANG_TOKEN", "TYPESAFE_API_KEY"})

    def test_secret_env_names_projection_covers_legacy(self):
        from pangpang.semantic_gate import secret_env_names
        names = secret_env_names({"connector": {"credential_env": "PANGPANG_DEVICE_TOKEN"}})
        self.assertIn("XIAN_PANGPANG_TOKEN", names)
        self.assertIn("XIAN_PANGPANG_DEVICE_TOKEN", names)

    def test_cli_resolve_access_token_accepts_legacy_name(self):
        from pangpang import cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["XIAN_PANGPANG_TOKEN"] = "l" * 40
        self.assertEqual(cli.resolve_access_token(Path(tmp.name), False, cli._parser()), "l" * 40)

    def test_cli_noninteractive_error_mentions_both_names(self):
        from pangpang import cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        err = __import__("io").StringIO()
        with patch.object(__import__("sys").stdout, "isatty", return_value=False), \
             __import__("contextlib").redirect_stderr(err):
            with self.assertRaises(SystemExit):
                cli.resolve_access_token(Path(tmp.name), False, cli._parser())
        text = err.getvalue()
        self.assertIn("PANGPANG_TOKEN", text)
        self.assertIn("XIAN_PANGPANG_TOKEN", text)

    def test_explicit_empty_new_name_never_falls_back_to_legacy(self):
        os.environ["XIAN_PANGPANG_TOKEN"] = "l" * 40
        os.environ["PANGPANG_TOKEN"] = ""
        self.assertEqual(env_value("PANGPANG_TOKEN", ""), "")

    def test_cli_rejects_explicit_empty_token_instead_of_using_legacy(self):
        from pangpang import cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["XIAN_PANGPANG_TOKEN"] = "l" * 40
        os.environ["PANGPANG_TOKEN"] = ""
        with patch.object(__import__("sys").stdout, "isatty", return_value=False):
            with self.assertRaises(SystemExit):
                cli.resolve_access_token(Path(tmp.name), False, cli._parser())


if __name__ == "__main__":
    unittest.main()
