"""Custom catalog rows stay on the host that owns them.

A row merged from this process's adapters file, or from another host's file
that landed in the same catalog, must not make a different device look ready
to configure. Configure on a device whose live catalog lacks the name reports
that absence, not a missing PATH entry.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_adapters_file import catalog_teardown
from test_bot_runtime import config
from pangpang import adapters_file
from pangpang.connector import Connector
from pangpang.execution import ADAPTER_CATALOG, Execution
from pangpang.service import Service
from pangpang.store import Fault


DESK = {"binary": "desk-tool", "argv": ["--stdio"], "concurrency": 1, "priority": 20,
        "use_when": "notes on this machine"}
PEER = {"binary": "peer-tool", "argv": ["--stdio"], "concurrency": 1, "priority": 30,
        "use_when": "notes on another machine"}


def _write_adapters(directory, rows):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "adapters.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    path.chmod(0o644)
    return path


class HostScopedCatalogTests(unittest.TestCase):
    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        cloud = self.root / "cloud"
        _write_adapters(cloud, {"desk-tool": DESK})
        cfg = config(self.workspace)
        cfg["installations"] = []
        cfg["hosts"] = [
            {"id": "host-local", "name": "cloud", "transport": "local", "os": "Linux",
             "allowed_roots": [str(self.workspace)]},
            {"id": "host-a", "name": "host-a", "transport": "connector", "os": "Linux",
             "allowed_roots": ["/srv/host-a"]},
            {"id": "gpu-linux", "name": "gpu", "transport": "connector", "os": "Linux",
             "allowed_roots": ["/srv/gpu"]},
        ]
        self.app = Service(cfg, cloud, deployment_level="L0")
        self.addCleanup(self.app.store.close)
        self.work_id = self.app.send("bot-main", {"text": "scope"}, "adapter-scope")["work_id"]

    def test_another_hosts_catalog_does_not_mark_this_device_known(self):
        self.assertIn("desk-tool", ADAPTER_CATALOG)
        self.assertTrue(adapters_file.is_merged("desk-tool"))
        self.assertTrue(self.app.adapter_known("host-local", "desk-tool"))
        self.assertTrue(self.app.adapter_known("gpu-linux", "codex"))
        self.assertFalse(self.app.adapter_known("gpu-linux", "desk-tool"))
        self.assertFalse(self.app.adapter_known("host-a", "desk-tool"))

        _write_adapters(self.root / "peer", {"peer-tool": PEER})
        adapters_file.merge(self.root / "peer" / "adapters.json")
        self.assertIn("peer-tool", ADAPTER_CATALOG)
        self.assertTrue(self.app.execution.knows_adapter("peer-tool", "host-local"))
        self.assertFalse(self.app.adapter_known("gpu-linux", "peer-tool"))
        self.assertFalse(self.app.execution.knows_adapter("peer-tool", "gpu-linux"))

        self.app.execution.set_declared_adapters("host-a", {
            "peer-tool": {"priority": 4, "use_when": PEER["use_when"], "concurrency": 1}})
        self.assertTrue(self.app.adapter_known("host-a", "peer-tool"))
        self.assertFalse(self.app.adapter_known("gpu-linux", "peer-tool"))
        self.assertFalse(self.app.adapter_known("gpu-linux", "desk-tool"))

        before = self.app.store.find("setup_request")
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.request("bot-main", self.work_id, {
                "host_id": "gpu-linux", "action": "configure", "adapter": "desk-tool"})
        self.assertEqual(ctx.exception.code, "adapter_required")
        self.assertNotEqual(ctx.exception.code, "cli_not_installed")
        with self.assertRaises(Fault) as proposed:
            self.app.propose_executor("bot-main", self.work_id, {
                "host_id": "gpu-linux", "adapter": "peer-tool"})
        self.assertEqual(proposed.exception.code, "unknown_adapter")
        with self.assertRaises(Fault) as operator:
            self.app.onboarding.operator_request({
                "host_id": "gpu-linux", "action": "configure", "adapter": "desk-tool"})
        self.assertEqual(operator.exception.code, "adapter_required")
        self.assertEqual(self.app.store.find("setup_request"), before)
        self.assertEqual(self.app.store.find("command", action="configure"), [])

        opened = self.app.onboarding.request("bot-main", self.work_id, {
            "host_id": "gpu-linux", "action": "configure", "adapter": "codex"})
        self.assertEqual(opened["status"], "needs_approval")

        gpu_row = {"id": "desk-tool-gpu-linux", "host_id": "gpu-linux", "adapter": "desk-tool",
                   "argv": ["device-owned-command"], "enabled": True, "risk": "write",
                   "cwd": "/srv/gpu"}
        candidate = json.loads(json.dumps(self.app.config))
        candidate["installations"] = [gpu_row]
        with self.assertRaises(Fault) as rejected:
            self.app.commit_managed_config(candidate)
        self.assertEqual(rejected.exception.code, "invalid_adapter")
        kept = Execution(candidate, self.app.data, tolerate_unknown_adapters=True)
        self.assertEqual(kept.installations["desk-tool-gpu-linux"]["adapter"], "desk-tool")
        self.assertEqual(kept.unknown_adapters["desk-tool-gpu-linux"]["reason"], "unknown_adapter")

        local_row = {"id": "desk-tool-host-local", "host_id": "host-local", "adapter": "desk-tool",
                     "argv": ["desk-tool", "--stdio"], "enabled": True, "risk": "write",
                     "cwd": str(self.workspace)}
        local_candidate = json.loads(json.dumps(self.app.config))
        local_candidate["installations"] = [local_row]
        self.app.commit_managed_config(local_candidate)
        self.assertNotIn("desk-tool-host-local", self.app.execution.unknown_adapters)
        self.assertEqual(self.app.execution.installations["desk-tool-host-local"]["adapter"], "desk-tool")

    def test_setup_result_names_the_catalog_miss(self):
        advice = ("adapter desk-tool is not in this host's live catalog; "
                  "declare it on this computer before configuring it")
        request_id = "setup-catalog-miss"
        self.app.store.put("setup_request", {
            "id": request_id, "host_id": "gpu-linux", "bot_id": "bot-main", "work_id": self.work_id,
            "status": "configuring", "proposal": {"action": "configure", "adapter": "desk-tool"},
        })
        self.app.onboarding.report({
            "id": "cmd-catalog", "action": "configure", "host_id": "gpu-linux",
            "payload": {"request_id": request_id, "adapter": "desk-tool"},
        }, {"error": "adapter_not_in_catalog", "advice": advice})
        bare_id = "setup-catalog-bare"
        self.app.store.put("setup_request", {
            "id": bare_id, "host_id": "gpu-linux", "bot_id": "bot-main", "work_id": self.work_id,
            "status": "configuring", "proposal": {"action": "configure", "adapter": "desk-tool"},
        })
        self.app.onboarding.report({
            "id": "cmd-catalog-bare", "action": "configure", "host_id": "gpu-linux",
            "payload": {"request_id": bare_id, "adapter": "desk-tool"},
        }, {"error": "adapter_not_in_catalog"})
        noted = [item["content"]["error"] for item in self.app.store.find("message", work_id=self.work_id)
                 if item.get("kind") == "setup_result" and item["content"].get("error")]
        self.assertIn("adapter_not_in_catalog: " + advice, noted)
        self.assertIn("adapter_not_in_catalog: adapter is not in this host's live catalog", noted)
        for text in noted:
            self.assertNotIn("cli_not_installed", text)
            self.assertNotIn("PATH", text)
        self.assertEqual(self.app.store.get("setup_request", request_id)["status"], "failed")
        self.assertEqual(self.app.store.get("setup_request", bare_id)["status"], "failed")


class DeviceCatalogMissTests(unittest.TestCase):
    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        device = config(self.workspace)
        device["installations"] = []
        device["hosts"][0]["id"] = "host-local"
        device.update(host_id="host-local", cloud_url="http://127.0.0.1:1",
                      credential_env="TEST_DEVICE_KEY")
        self.connector = Connector(device, Path(self.tmp.name) / "device")
        self.addCleanup(self.connector.close)

    def test_configure_of_an_unknown_adapter_is_not_a_missing_cli(self):
        def fail_which(binary):
            raise AssertionError("PATH lookup for an adapter this host does not know: %s" % binary)

        with patch("pangpang.local_setup.shutil.which", side_effect=fail_which):
            self.connector.execute({
                "id": "cmd-unknown-adapter",
                "action": "configure",
                "payload": {"adapter": "desk-tool", "cwd": str(self.workspace)},
            })
        result = self.connector.store.get("local_command", "cmd-unknown-adapter")["result"]
        self.assertEqual(result["error"], "adapter_not_in_catalog")
        self.assertIn("desk-tool", result["advice"])
        self.assertIn("live catalog", result["advice"])
        self.assertNotIn("cli_not_installed", result["advice"])
        self.assertNotIn("PATH", result["advice"])
        self.assertNotIn("not installed", result["advice"])
        self.assertNotIn("desk-tool-host-local", self.connector.execution.installations)

        with patch("pangpang.local_setup.fetch_pinned_pi", return_value=None), \
                patch("pangpang.local_setup.shutil.which", return_value=None):
            self.connector.execute({
                "id": "cmd-missing-cli",
                "action": "configure",
                "payload": {"adapter": "codex", "cwd": str(self.workspace)},
            })
        missing = self.connector.store.get("local_command", "cmd-missing-cli")["result"]
        self.assertEqual(missing["error"], "cli_not_installed")
        self.assertIn("codex", missing["advice"])
        self.assertIn("PATH", missing["advice"])


if __name__ == "__main__":
    unittest.main()
