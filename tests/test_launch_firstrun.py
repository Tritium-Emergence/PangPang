"""The first-run secret contract and the frozen product entry: a frozen build
must reuse launch.py's single first-run logic (interactive generate-once / non-interactive
fail-closed) and dispatch in-process, not bypass it by importing pangpang.__main__ directly."""
import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launch
from pangpang.vault import Vault


class FirstRunTokenContractTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ)
        self.env.start()
        os.environ.pop("PANGPANG_TOKEN", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        self.env.stop()

    def test_existing_env_token_wins_without_touching_vault(self):
        os.environ["PANGPANG_TOKEN"] = "x" * 30
        with patch("pangpang.vault.secure_directory", return_value=self.data) as secure, \
             patch("pangpang.vault.Vault") as V:
            self.assertEqual(launch.resolve_access_token(self.data, False, argparse.ArgumentParser()), "x" * 30)
        secure.assert_called_once_with(self.data)
        V.assert_not_called()

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privilege")
    def test_data_root_symlink_is_rejected_before_token_or_vault_use(self):
        target = self.data / "real-data"
        target.mkdir()
        alias = self.data / "alias-data"
        alias.symlink_to(target, target_is_directory=True)
        os.environ["PANGPANG_TOKEN"] = "x" * 30
        with patch("pangpang.vault.Vault") as V:
            with self.assertRaisesRegex(RuntimeError, "unsafe_data_directory"):
                launch.resolve_access_token(alias, False, argparse.ArgumentParser())
        V.assert_not_called()

    def test_noninteractive_first_run_fails_closed(self):
        # No token, no tty -> must error, never silently provision a secret.
        with patch.object(sys.stdout, "isatty", return_value=False):
            with self.assertRaises(SystemExit):
                launch.resolve_access_token(self.data, False, argparse.ArgumentParser())
        # And nothing was persisted.
        v = Vault(self.data / "vault")
        try:
            self.assertFalse(v.has("web-access-token"))
        finally:
            v.close()

    def test_interactive_first_run_generates_once_and_persists(self):
        with patch.object(sys.stdout, "isatty", return_value=True):
            token = launch.resolve_access_token(self.data, False, argparse.ArgumentParser())
        self.assertGreaterEqual(len(token), 24)
        # Second call returns the SAME persisted token (generate-once).
        os.environ.pop("PANGPANG_TOKEN", None)
        with patch.object(sys.stdout, "isatty", return_value=True):
            again = launch.resolve_access_token(self.data, False, argparse.ArgumentParser())
        self.assertEqual(token, again)


class FrozenEntryTests(unittest.TestCase):
    def test_frozen_cloud_runs_token_contract_then_dispatches_in_process(self):
        # The frozen entry must run the first-run token contract (so a clean user never needs
        # to know PANGPANG_TOKEN) and then call pangpang.__main__.main in-process -- not
        # re-exec a system interpreter, and not bypass launch's first-run logic.
        args = argparse.Namespace(role="cloud", data=None, show_access_key=False)
        with patch("pangpang.pi_worker_command.packaged_worker_command", return_value=["bun"]), \
             patch("pangpang.cli.resolve_access_token", return_value="t" * 30) as tok, \
             patch("pangpang.__main__.main", return_value=0) as role_main:
            rc = launch.run_frozen(args, ["--port", "8790"], argparse.ArgumentParser())
        self.assertEqual(rc, 0)
        tok.assert_called_once()
        role_main.assert_called_once()
        self.assertEqual(os.environ["PANGPANG_TOKEN"], "t" * 30)
        from pangpang.cli import source_root
        self.assertEqual(sys.argv, ["pangpang", "--data", str((source_root() / "data-v2").absolute()), "--port", "8790"])

    def test_frozen_cloud_fails_closed_on_corrupt_release_bundle_never_node(self):
        # A missing/corrupt release bun must fail startup, not defer to first exchange and
        # never fall through to Node. packaged_worker_command raises -> parser.error (SystemExit).
        args = argparse.Namespace(role="cloud", data=None, show_access_key=False)
        with patch("pangpang.pi_worker_command.packaged_worker_command",
                   side_effect=ValueError("pi_worker_incomplete_release_bundle")), \
             patch("pangpang.cli.resolve_access_token") as tok, \
             patch("pangpang.__main__.main") as role_main:
            with self.assertRaises(SystemExit):
                launch.run_frozen(args, [], argparse.ArgumentParser())
        tok.assert_not_called() # never reached the token contract
        role_main.assert_not_called() # never started the coordinator, never Node

    def test_frozen_cloud_absent_release_dir_fails_closed_via_real_resolver(self):
        # Same red line but through the REAL resolver (no stub): sys.frozen=True + an absent
        # runtime/pi-worker directory must fail startup, not return None and start anyway.
        import pangpang.pi_worker_command as pwc
        args = argparse.Namespace(role="cloud", data=None, show_access_key=False)
        with patch("pangpang.pi_worker_command.release_root", return_value=Path(tempfile.gettempdir()) / "xpp-absent-release"), \
             patch.object(pwc.sys, "frozen", True, create=True), \
             patch("pangpang.cli.resolve_access_token") as tok, \
             patch("pangpang.__main__.main") as role_main:
            with self.assertRaises(SystemExit):
                launch.run_frozen(args, [], argparse.ArgumentParser())
        tok.assert_not_called()
        role_main.assert_not_called()

    def test_frozen_connector_does_not_require_token(self):
        args = argparse.Namespace(role="connector", data=None, show_access_key=False)
        with patch("pangpang.cli.resolve_access_token") as tok, \
             patch("pangpang.connector.main", return_value=0) as role_main:
            rc = launch.run_frozen(args, [], argparse.ArgumentParser())
        self.assertEqual(rc, 0)
        tok.assert_not_called() # connector has no web token contract
        role_main.assert_called_once()


class LaunchPrereqHintTests(unittest.TestCase):
    """Version errors name a fix. They never run the printed command."""

    def test_old_python_error_names_platform_fixes(self):
        cwd = os.getcwd()
        err = io.StringIO()
        try:
            with patch("sys.argv", ["launch.py", "cloud"]), \
                    patch.object(sys, "version_info", (3, 10, 0)), \
                    contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as caught:
                    launch.main()
        finally:
            os.chdir(cwd)
        self.assertEqual(caught.exception.code, 2)
        text = err.getvalue()
        self.assertIn("Python 3.11+ required", text)
        self.assertIn("brew install python@3.13", text)
        self.assertIn("apt install python3 python3-venv", text)
        self.assertIn("winget install -e --id Python.Python.3.13", text)

    def test_old_node_error_names_platform_fixes(self):
        root = Path(launch.__file__).resolve().parent
        err = io.StringIO()
        with patch("pangpang.pi_worker_command.packaged_worker_command", return_value=None), \
                patch("pangpang.cli.subprocess.check_output", return_value="v20.18.0\n"), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                launch.ensure_model_runtime(root, root / ".venv", argparse.ArgumentParser())
        self.assertEqual(caught.exception.code, 2)
        text = err.getvalue()
        self.assertIn("Node.js >=22.19", text)
        self.assertIn("brew install node@22", text)
        self.assertIn("apt install nodejs npm", text)
        self.assertIn("NodeSource", text)
        self.assertIn("winget install -e --id OpenJS.NodeJS.22", text)
        self.assertNotIn("curl", text.lower())


if __name__ == "__main__":
    unittest.main()
