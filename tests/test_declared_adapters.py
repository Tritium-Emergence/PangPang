"""Connector-declared adapters: the cloud routes on the device's declaration.

A connector host owns its adapters.json; the cloud must work without a copy.
These tests prove the wire projection, host-scoped membership, lifecycle
(withdrawal quarantines but never deletes), and one execution end to end
with a cloud data directory that never contained an adapters.json.
"""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

from test_adapters_file import ROW, catalog_teardown
from test_bot_runtime import config
from pangpang import adapters_file
from pangpang.connector import Connector, policy_digest
from pangpang.execution import ADAPTER_CATALOG, installation_id
from pangpang.service import Service
from pangpang.store import Fault

HOST = "local"
IID = "opencode-test-local"


class ValidateDeclaredTests(unittest.TestCase):
    def assert_rejected(self, payload):
        with self.assertRaises(Fault) as ctx:
            adapters_file.validate_declared(payload)
        self.assertEqual(ctx.exception.code, "invalid_declared_adapters")

    def test_absent_is_legacy_not_empty(self):
        self.assertIsNone(adapters_file.validate_declared(None))
        self.assertEqual(adapters_file.validate_declared({}), {})

    def test_routing_fields_pass_unchanged(self):
        declared = {"opencode-test": {"concurrency": 2, "priority": 15, "use_when": "fast"}}
        self.assertEqual(adapters_file.validate_declared(declared), declared)

    def test_optional_fields_may_be_absent(self):
        self.assertEqual(adapters_file.validate_declared({"opencode-test": {}}), {"opencode-test": {}})

    def test_execution_and_env_material_never_cross(self):
        for row in ({"argv": ["rm", "-rf", "/"]}, {"binary": "sh"},
                    {"secret_env": "OPENAI_API_KEY"}, {"extra_env": {"A": "b"}},
                    {"stdin_protocol": "x"}, {"interjection_tier": 1}):
            with self.subTest(row=row):
                self.assert_rejected({"opencode-test": dict(row)})

    def test_name_shape_and_builtin_collision_rejected_on_the_wire(self):
        for name in ("Bad_Name", "x" * 33, "1abc", "", 5):
            self.assert_rejected({name: {}})
        self.assertEqual(list(adapters_file.validate_declared({"x" * 32: {}})), ["x" * 32])
        for name in ("codex", "claude-code"):
            self.assert_rejected({name: {"priority": 1}})

    def test_value_bounds(self):
        for bad in ({"opencode-test": {"concurrency": True}}, {"opencode-test": {"concurrency": 0}},
                    {"opencode-test": {"concurrency": 65}}, {"opencode-test": {"priority": -1}},
                    {"opencode-test": {"priority": 1001}}, {"opencode-test": {"use_when": "  "}},
                    {"opencode-test": {"use_when": "x" * 501}}, {"opencode-test": 5}):
            with self.subTest(bad=bad):
                self.assert_rejected(bad)
        self.assert_rejected({("a-%d" % i): {} for i in range(adapters_file.MAX_ROWS + 1)})
        self.assert_rejected({"opencode-test": {"use_when": "y" * 300000}})
        self.assert_rejected(["not", "a", "map"])


class DeclaredSyncTests(unittest.TestCase):
    """Broker ingest and host-scoped membership without a full connector."""

    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "workspace"
        root.mkdir()
        cloud = config(root)
        cloud["hosts"][0]["transport"] = "connector"
        cloud["installations"] = []
        self.app = Service(cloud, Path(self.tmp.name) / "cloud")
        # Probe traffic is exercised end to end by the e2e class below.
        self.app._auto_probe_installation = lambda installation_id: None
        self.addCleanup(self.app.store.close)

    def sync(self, body):
        payload = {"instance_id": "device-" + "0" * 32}
        payload.update(body)
        return self.app.broker.sync(HOST, payload)

    def device_record(self):
        return self.app.store.get("device", HOST)

    def test_snapshot_persists_and_absent_leaves_it_alone(self):
        self.sync({"declared_adapters": {"opencode-test": {"priority": 15}}})
        self.assertEqual(self.device_record()["declared_adapters"], {"opencode-test": {"priority": 15}})
        self.assertEqual(self.app.execution.declared_adapters[HOST], {"opencode-test": {"priority": 15}})
        self.sync({"version": "9.9.9"})  # a legacy device omits the field
        self.assertEqual(self.device_record()["declared_adapters"], {"opencode-test": {"priority": 15}})

    def test_explicit_withdrawal_quarantines_and_rejects_bad_payloads(self):
        self.sync({"declared_adapters": {"opencode-test": {"concurrency": 2}}})
        candidate = copy.deepcopy(self.app.config)
        candidate["installations"] = [{"id": IID, "host_id": HOST, "adapter": "opencode-test",
                                       "argv": ["device-owned-command"], "enabled": True, "risk": "write"}]
        self.app.commit_managed_config(candidate)
        self.assertNotIn(IID, self.app.execution.unknown_adapters)
        with self.assertRaises(Fault) as ctx:
            self.sync({"declared_adapters": {"opencode-test": {"argv": ["evil"]}}})
        self.assertEqual(ctx.exception.code, "invalid_declared_adapters")
        # Rejected on arrival: the previous snapshot survives untouched.
        self.assertEqual(self.device_record()["declared_adapters"], {"opencode-test": {"concurrency": 2}})
        self.assertNotIn(IID, self.app.execution.unknown_adapters)
        self.sync({"declared_adapters": {}})
        self.assertEqual(self.device_record()["declared_adapters"], {})
        marked = self.app.execution.unknown_adapters[IID]
        self.assertEqual(marked["reason"], "unknown_adapter")
        # The remedy for a connector host is device-side, not a cloud file copy.
        self.assertIn("this device declares", marked["advice"])
        self.assertIn(IID, self.app.execution.installations)  # never deleted

    def test_declarations_never_cross_hosts(self):
        self.sync({"declared_adapters": {"opencode-test": {}}})
        other = {"id": "other", "transport": "connector", "os": "Linux", "allowed_roots": ["/srv/other"]}
        candidate = copy.deepcopy(self.app.config)
        candidate["hosts"] = list(candidate["hosts"]) + [other]
        candidate["installations"] = [{"id": "opencode-other", "host_id": "other", "adapter": "opencode-test",
                                       "argv": ["device-owned-command"], "enabled": True, "risk": "write"}]
        # Fail-closed: host "other" never declared it, so registering that row
        # is refused — host "local"'s declaration never leaks across hosts.
        with self.assertRaises(Fault) as ctx:
            self.app.commit_managed_config(candidate)
        self.assertEqual(ctx.exception.code, "invalid_adapter")

    def test_routing_hints_come_from_the_declaration(self):
        self.sync({"declared_adapters": {"opencode-test": {"priority": 3, "use_when": "长文本润色。"}}})
        candidate = copy.deepcopy(self.app.config)
        candidate["installations"] = [{"id": IID, "host_id": HOST, "adapter": "opencode-test",
                                       "argv": ["device-owned-command"], "enabled": True, "risk": "write"}]
        self.app.commit_managed_config(candidate)
        inst = self.app.execution.installations[IID]
        self.assertEqual(self.app._route_priority(inst), 3)
        self.assertEqual(self.app._use_when(inst), "长文本润色。")
        # An explicit installation-level override still wins.
        self.assertEqual(self.app._route_priority({**inst, "priority": 42}), 42)
        listed = {row["installation_id"]: row for row in self.app.list_executors()["installations"]}[IID]
        self.assertEqual(listed["priority"], 3)
        self.assertEqual(listed["use_when"], "长文本润色。")
        # Declared names join the adapter list without a cloud-side catalog row.
        self.assertIn("opencode-test", {row["adapter"] for row in self.app.list_executors()["adapters"]})


class DeclaredAdapterEndToEndTests(unittest.TestCase):
    """The whole point: an operator writes adapters.json on ONE machine — the
    device — and a task runs without the cloud ever holding a copy."""

    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "workspace"
        self.root.mkdir()
        device_data = Path(self.tmp.name) / "device"
        cloud_data = Path(self.tmp.name) / "cloud"
        device_data.mkdir()
        path = device_data / "adapters.json"
        path.write_text(json.dumps({"opencode-test": ROW}), encoding="utf-8")
        path.chmod(0o644)
        cloud_data.mkdir()  # deliberately no adapters.json on the cloud

        local = config(self.root)
        local["installations"][0].update(id=IID, adapter="opencode-test", enabled=True)
        local.update(host_id=HOST, cloud_url="http://127.0.0.1:1", credential_env="TEST_DEVICE_KEY")
        self.connector = Connector(local, device_data)
        self.assertEqual(self.connector.declared_adapters,
                         {"opencode-test": {"concurrency": ROW["concurrency"],
                                            "priority": ROW["priority"], "use_when": ROW["use_when"]}})

        cloud = config(self.root)
        cloud["hosts"][0].update(transport="connector", credential_env="TEST_DEVICE_KEY")
        cloud["installations"] = []
        self.app = Service(cloud, cloud_data)
        self.connector.request = lambda body: self.app.broker.sync(HOST, body)
        self.connector.tick()
        self.assertEqual(self.app.store.get("device", HOST)["declared_adapters"],
                         {"opencode-test": {"concurrency": ROW["concurrency"],
                                            "priority": ROW["priority"], "use_when": ROW["use_when"]}})

    def tearDown(self):
        for run in self.connector.store.find("local_run", status="active"):
            try:
                self.connector.execution.cancel(run["task"])
            except Exception:
                pass
        self.connector.close()
        self.app.store.close()
        self.tmp.cleanup()

    def test_declared_adapter_runs_without_any_cloud_side_file(self):
        # Declaration alone registered the row — no cloud-side file, no operator
        # approval round trip. Tell once (adapters.json), use directly.
        self.assertEqual(IID, installation_id("opencode-test", HOST, None))
        inst = self.app.execution.installations[IID]
        self.assertEqual(inst["argv"], ["device-owned-command"])
        self.assertEqual(inst["max_concurrency"], ROW["concurrency"])  # declared, not 1
        self.assertEqual(inst["cwd"], self.app.execution.hosts[HOST]["allowed_roots"][0])
        self.assertEqual(self.app._route_priority(inst), ROW["priority"])
        self.assertEqual(self.app._use_when(inst), ROW["use_when"])
        self.assertNotIn(IID, self.app.execution.unknown_adapters)
        self.assertFalse((self.app.data / "adapters.json").exists())

        # Discovery evidence arrives on the existing probe command; the ticks
        # below are what let the cloud's background probe complete.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self.connector.tick()
            try:
                if self.app.store.get("probe", IID).get("available"):
                    break
            except Fault:
                pass
            time.sleep(.05)
        else:
            self.fail("the auto-probe never landed")

        m = self.app.send("bot-main", {"text": "制作结果", "allow_write": True}, "declared-work")
        r = self.app.submit_task("bot-main", m["work_id"],
                                 {"prompt": "中文内容", "outputs": ["result.txt"]})
        self.app.task_step()
        task = self.app.store.get("task", r["task_id"])
        self.assertEqual(task["installation_id"], IID)
        # Consent invariant survives declaration: the first run on a freshly
        # landed installation still asks a human, and the human says yes.
        self.assertEqual(task["status"], "awaiting_approval")
        self.app.approve(task["id"], {"spec_digest": task["spec_digest"]})
        self.app.task_step()
        task = self.app.store.get("task", r["task_id"])
        command = [c for c in self.app.store.find("command", action="start", limit=1000)
                   if c.get("payload", {}).get("execution_id") == task["execution_id"]][0]
        self.connector.execute({k: command[k] for k in ("id", "action", "payload", "expires")})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.connector.tick()
            current = self.app.store.get("task", task["id"])
            if current["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(.05)
        self.assertEqual(current["status"], "succeeded", current)
        self.assertTrue((self.root / "result.txt").exists())

        # Withdrawal: new work is refused; the installation row survives.
        # The shared-process test lets the device's merge leak into the global
        # catalog the cloud also consults; production processes are separate,
        # so hide the row for the withdrawal tick to model that faithfully.
        saved = ADAPTER_CATALOG.pop("opencode-test")
        try:
            self.connector.declared_adapters = {}
            self.connector.tick()
        finally:
            ADAPTER_CATALOG["opencode-test"] = saved
        marked = self.app.execution.unknown_adapters[IID]
        self.assertEqual(marked["reason"], "unknown_adapter")
        self.assertIn("this device declares", marked["advice"])
        self.assertIn(IID, self.app.execution.installations)
        route = self.app.route("bot-main", {"id": "declared-work"}, {})
        self.assertEqual({row["installation_id"]: row["reason"] for row in route["rejected"]}[IID],
                         "unknown_adapter")


if __name__ == "__main__":
    unittest.main()
