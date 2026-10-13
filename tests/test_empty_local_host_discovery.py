"""Restart PATH discovery when host-local exists and has no installations.

A local host row is not discovery evidence. With zero installations there is
no installation id for the operator probe to target, so a CLI that appears on
PATH later must be registered by the same scan first launch uses. A host that
already has an installation stays one-shot.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pangpang.onboarding import empty_config
from pangpang.service import Service


class EmptyLocalHostDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        cfg = empty_config()
        cfg["hosts"] = [{
            "id": "host-local", "name": "本机", "transport": "local",
            "os": "Linux", "allowed_roots": [str(self.workspace)],
        }]
        self.app = Service(cfg, self.root / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_restart_registers_cli_when_local_host_has_no_installations(self):
        def which(binary):
            return sys.executable if binary == "claude" else None

        with patch("pangpang.service.shutil.which", side_effect=which):
            result = self.app.ensure_local_host(str(self.workspace), discover=True)

        self.assertEqual(result.get("skipped"), None)
        self.assertEqual(result["installations"], ["inst-local-claude-code"])
        inst = self.app.execution.installations["inst-local-claude-code"]
        self.assertEqual(inst["host_id"], "host-local")
        self.assertEqual(inst["adapter"], "claude-code")
        self.assertEqual(inst["argv"][0], sys.executable)
        self.assertNotIn("inst-local-legacy-executor", self.app.execution.installations)
        self.assertNotIn("inst-local-codex", self.app.execution.installations)
        self.assertEqual(
            [h["id"] for h in self.app.config["hosts"] if h.get("transport") == "local"],
            ["host-local"])
        probe = self.app.store.get("probe", "inst-local-claude-code")
        self.assertTrue(probe.get("available"))

        # Once any installation exists, a later CLI on PATH is not auto-added.
        def which_all(_binary):
            return sys.executable

        with patch("pangpang.service.shutil.which", side_effect=which_all):
            again = self.app.ensure_local_host(str(self.workspace), discover=True)
        self.assertEqual(again.get("skipped"), "already_registered")
        self.assertEqual(list(self.app.execution.installations), ["inst-local-claude-code"])
