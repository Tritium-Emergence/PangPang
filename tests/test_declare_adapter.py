"""Operator approval is what lets a connector store one custom adapter row."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pangpang import adapters_file
from pangpang.execution import ADAPTER_CATALOG, Execution
from pangpang.local_setup import LocalSetup
from pangpang.onboarding import empty_config
from pangpang.service import Service
from pangpang.store import Fault, Store, uid
from pangpang.vault import Vault
from test_adapters_file import catalog_teardown

ROW = {"binary": "my-echo", "argv": ["--stdio"], "concurrency": 1, "priority": 20,
       "use_when": "echo a line"}
OTHER = {"binary": "legacy-private-cli", "argv": ["--legacy"], "concurrency": 1,
         "priority": 30, "use_when": "kept row"}


class DeclareAdapterTests(unittest.TestCase):
    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.cloud_data = self.root / "cloud"
        self.device_data = self.root / "device"
        self.cloud_data.mkdir()
        self.device_data.mkdir()
        self.app = Service(empty_config(), self.cloud_data)
        self.addCleanup(self.app.store.close)
        self.addCleanup(self.tmp.cleanup)
        self.codex_argv = list(ADAPTER_CATALOG["codex"]["argv"])
        self.host_id = self.paired_host()
        self.work_id = self.app.send("bot-main", {"text": "declare"}, "declare-adapter")["work_id"]

    def paired_host(self):
        store = Store(self.device_data / "connector.db")
        identity = uid("device")
        store.put("identity", {"id": "device", "instance_id": identity})
        store.close()
        vault = Vault(self.device_data / "vault")
        pub = vault.public_key()
        vault.close()
        code = self.app.onboarding.pairing_code()["code"]
        pair = self.app.onboarding.pair({"code": code, "instance_id": identity, "public_key": pub,
                                         "name": "PC", "os": "Linux"})
        return pair["host_id"]

    def body(self, **extra):
        fields = {"host_id": self.host_id, "action": "declare_adapter", "adapter": "my-echo", **ROW}
        fields.update(extra)
        return fields

    def request(self, **extra):
        return self.app.onboarding.request("bot-main", self.work_id, self.body(**extra))

    def commands(self):
        return self.app.store.find("command", action="declare_adapter")

    def device(self):
        live = {"host_id": "local",
                "hosts": [{"id": "local", "transport": "local", "os": "Linux",
                           "allowed_roots": [str(self.workspace)]}],
                "installations": [], "resources": []}
        store = Store(self.device_data / "setup.db")
        self.addCleanup(store.close)
        holder = SimpleNamespace(store=store, data=self.device_data, config=live,
                                 config_revision=0, execution=Execution(live, self.device_data),
                                 host_id="local")
        return LocalSetup(holder), holder

    def forget_catalog(self, *names):
        for name in names:
            ADAPTER_CATALOG.pop(name, None)
            adapters_file._merged_names.discard(name)

    def test_unknown_name_fails_until_the_approved_row_is_reported(self):
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.request("bot-main", self.work_id, {
                "host_id": self.host_id, "action": "configure", "adapter": "my-echo"})
        self.assertEqual(ctx.exception.code, "adapter_required")
        self.assertFalse((self.cloud_data / "adapters.json").exists())
        self.assertFalse((self.device_data / "adapters.json").exists())
        self.assertNotIn("my-echo", ADAPTER_CATALOG)

        staged = self.request()
        self.assertEqual(staged["status"], "needs_approval")
        proposal = self.app.store.get("setup_request", staged["request_id"])["proposal"]
        self.assertEqual(proposal["command"], ["my-echo", "--stdio"])
        self.assertEqual(proposal["adapter_row"], ROW)
        self.assertNotIn("origin", self.app.store.get("setup_request", staged["request_id"]))
        self.assertEqual(self.commands(), [])
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.request("bot-main", self.work_id, {
                "host_id": self.host_id, "action": "configure", "adapter": "my-echo"})
        self.assertEqual(ctx.exception.code, "adapter_required")

        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(staged["request_id"], {"endpoint": "https://example.com"})
        self.assertEqual(ctx.exception.code, "invalid_setup_approval")
        self.assertEqual(self.commands(), [])

        self.app.onboarding.approve(staged["request_id"], {})
        command = self.commands()[0]
        self.assertIsNone(command["expires"])
        self.assertEqual(command["payload"]["row"], ROW)
        self.assertNotIn("argv", command["payload"])
        self.assertFalse((self.cloud_data / "adapters.json").exists())
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"], self.codex_argv)

        setup, holder = self.device()
        reported = setup.declare_adapter({"adapter": "my-echo", "row": command["payload"]["row"]})
        again = setup.declare_adapter({"adapter": "my-echo", "row": dict(ROW)})
        self.assertEqual(again, reported)
        stored = json.loads((self.device_data / "adapters.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["my-echo"], ROW)
        self.assertFalse((self.device_data / ".adapters.json.tmp").exists())
        with patch("pangpang.local_setup.shutil.which", return_value="/usr/local/bin/my-echo"), \
                patch("pangpang.local_setup.probe", return_value={"version": "1"}):
            setup.configure({"adapter": "my-echo"})
        self.assertEqual(holder.execution.installations["my-echo-local"]["argv"],
                         ["/usr/local/bin/my-echo", "--stdio"])

        self.forget_catalog("my-echo")
        self.app.onboarding.report(command, reported)
        self.assertEqual(self.app.store.get("declared_adapters", self.host_id)["rows"], {"my-echo": ROW})
        self.assertTrue(self.app.adapter_known(self.host_id, "my-echo"))
        self.assertNotIn("my-echo", ADAPTER_CATALOG)
        self.assertFalse((self.cloud_data / "adapters.json").exists())
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"], self.codex_argv)
        names = [item["adapter"] for item in self.app.list_executors()["adapters"]]
        self.assertIn("my-echo", names)
        self.assertNotIn("argv", self.app.list_executors()["adapters"][0])

        accepted = self.app.onboarding.request("bot-main", self.work_id, {
            "host_id": self.host_id, "action": "configure", "adapter": "my-echo"})
        self.assertEqual(accepted["status"], "needs_approval")
        self.app.onboarding.approve(accepted["request_id"], {})
        configure = [c for c in self.app.store.find("command", action="configure")
                     if c["payload"].get("request_id") == accepted["request_id"]][0]
        self.assertNotIn("argv", configure["payload"])
        self.assertNotIn("row", configure["payload"])
        self.app.onboarding.report(configure, {"installation": {
            "id": "my-echo-" + self.host_id, "host_id": self.host_id, "adapter": "my-echo",
            "cwd": "/work", "max_concurrency": 1, "resource_keys": []}})
        landed = self.app.execution.installations["my-echo-" + self.host_id]
        self.assertEqual(landed["argv"], ["device-owned-command"])
        self.assertEqual(landed["use_when"], "echo a line")
        self.assertEqual(landed["priority"], 20)
        self.assertEqual(landed["workspace_key"][0], self.host_id)
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"], self.codex_argv)

    def test_discover_does_not_import_a_row(self):
        self.app.onboarding.report(
            {"action": "discover", "host_id": self.host_id, "payload": {}, "id": "disc"},
            {"inventory": [], "declared_adapters": [{"adapter": "my-echo", **ROW}]})
        self.assertEqual(self.app.store.find("declared_adapters"), [])
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.request("bot-main", self.work_id, {
                "host_id": self.host_id, "action": "configure", "adapter": "my-echo"})
        self.assertEqual(ctx.exception.code, "adapter_required")

    def test_forbidden_and_builtin_rows_are_rejected(self):
        before = self.app.store.find("setup_request")
        for key in ("pinned", "sha256", "interjection_tier", "stdin_protocol", "session_protocol",
                    "resume_protocol", "permission_bridge", "models_json", "inject"):
            with self.subTest(key=key):
                with self.assertRaises(Fault) as ctx:
                    self.request(**{key: "x"})
                self.assertEqual(ctx.exception.code, "invalid_adapter_declaration")
                self.assertIn(key, ctx.exception.advice)
                with self.assertRaises(Fault) as ctx:
                    adapters_file.write_merged_row(self.device_data / "adapters.json", "my-echo",
                                                   dict(ROW, **{key: "x"}))
                self.assertEqual(ctx.exception.code, "invalid_adapter_declaration")
                self.assertIn(key, ctx.exception.advice)
        self.assertEqual(self.app.store.find("setup_request"), before)
        self.assertFalse((self.device_data / "adapters.json").exists())
        with self.assertRaises(Fault) as ctx:
            self.request(adapter="codex")
        self.assertEqual(ctx.exception.code, "invalid_adapter_declaration")
        self.assertIn("built-in", ctx.exception.advice)
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"], self.codex_argv)
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.request("bot-main", self.work_id, {
                "host_id": self.host_id, "action": "configure", "adapter": "codex", "argv": ["--x"]})
        self.assertEqual(ctx.exception.code, "invalid_setup_request_field")
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.request("bot-main", self.work_id, self.body(host_id="local"))
        self.assertEqual(ctx.exception.code, "connector_host_required")
        self.assertEqual(self.app.store.find("setup_request"), before)

    def test_mismatched_report_is_not_stored(self):
        staged = self.request()
        self.app.onboarding.approve(staged["request_id"], {})
        command = self.commands()[0]
        swapped = dict(ROW, argv=["--swapped"])
        self.app.onboarding.report(command, {"declared_adapters": [
            {"adapter": "my-echo", **swapped},
            {"adapter": "legacy-private-cli", **OTHER}]})
        self.assertEqual(self.app.store.get("setup_request", staged["request_id"])["status"], "failed")
        self.assertEqual(self.app.store.find("declared_adapters"), [])
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.request("bot-main", self.work_id, {
                "host_id": self.host_id, "action": "configure", "adapter": "my-echo"})
        self.assertEqual(ctx.exception.code, "adapter_required")

    def test_sibling_report_is_not_imported(self):
        staged = self.request()
        self.app.onboarding.approve(staged["request_id"], {})
        command = self.commands()[0]
        self.app.onboarding.report(command, {"declared_adapters": [
            {"adapter": "legacy-private-cli", **OTHER},
            {"adapter": "my-echo", **ROW}]})
        self.assertEqual(self.app.store.get("setup_request", staged["request_id"])["status"], "done")
        self.assertEqual(set(self.app.store.get("declared_adapters", self.host_id)["rows"]), {"my-echo"})
        self.assertFalse(self.app.adapter_known(self.host_id, "legacy-private-cli"))

    def test_device_conflict_keeps_the_stored_row(self):
        path = self.device_data / "adapters.json"
        path.write_text(json.dumps({"my-echo": dict(ROW, argv=["--old"]),
                                    "legacy-private-cli": OTHER}), encoding="utf-8")
        path.chmod(0o644)
        before = path.read_text(encoding="utf-8")
        staged = self.request()
        self.app.onboarding.approve(staged["request_id"], {})
        command = self.commands()[0]
        setup, _holder = self.device()
        with self.assertRaises(Fault) as ctx:
            setup.declare_adapter({"adapter": "my-echo", "row": command["payload"]["row"]})
        self.assertEqual(ctx.exception.code, "adapter_declaration_conflict")
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertNotIn("my-echo", ADAPTER_CATALOG)
        self.app.onboarding.report(command, {"error": "adapter_declaration_conflict", "advice": ctx.exception.advice})
        self.assertEqual(self.app.store.get("setup_request", staged["request_id"])["status"], "failed")
        self.assertEqual(self.app.store.find("declared_adapters"), [])
        self.assertFalse((self.cloud_data / "adapters.json").exists())

    def sync_report(self, command, result):
        device = self.app.store.get("device", self.host_id)
        return self.app.broker.sync(self.host_id, {
            "instance_id": device["instance_id"],
            "supported_contract_version": 3,
            "reports": [{"id": command["id"], "result": result}],
            "receipts": [],
        })

    def test_sync_drops_a_swapped_report_from_the_command_result(self):
        staged = self.request()
        self.app.onboarding.approve(staged["request_id"], {})
        command = self.commands()[0]
        swapped = dict(ROW, argv=["--swapped"], extra_env={"TOKEN": "secret-value"})
        sibling = dict(OTHER, extra_env={"TOKEN": "secret-value"})
        raw = {"declared_adapters": [{"adapter": "my-echo", **swapped},
                                     {"adapter": "legacy-private-cli", **sibling}]}
        self.sync_report(command, raw)
        stored = self.app.store.get("command", command["id"])["result"]
        blob = json.dumps(stored)
        self.assertNotIn("--swapped", blob)
        self.assertNotIn("secret-value", blob)
        self.assertNotIn("legacy-private-cli", blob)
        self.assertNotIn("declared_adapters", stored)
        self.assertEqual(stored["error"], "adapter_declaration_mismatch")
        self.assertEqual(self.app.store.find("declared_adapters"), [])
        self.assertEqual(self.app.store.get("setup_request", staged["request_id"])["status"], "failed")
        self.sync_report(command, raw)
        self.assertEqual(self.app.store.get("command", command["id"])["result"], stored)
        with self.assertRaises(Fault) as ctx:
            self.sync_report(command, {"declared_adapters": [{"adapter": "my-echo", **ROW}]})
        self.assertEqual(ctx.exception.code, "command_result_conflict")
        self.assertEqual(self.app.store.get("command", command["id"])["result"], stored)

    def test_sync_sibling_stays_out_of_the_command_result(self):
        staged = self.request()
        self.app.onboarding.approve(staged["request_id"], {})
        command = self.commands()[0]
        sibling = dict(OTHER, extra_env={"TOKEN": "secret-value"})
        raw = {"declared_adapters": [{"adapter": "legacy-private-cli", **sibling},
                                     {"adapter": "my-echo", **ROW}]}
        self.sync_report(command, raw)
        stored = self.app.store.get("command", command["id"])["result"]
        blob = json.dumps(stored)
        self.assertNotIn("legacy-private-cli", blob)
        self.assertNotIn("secret-value", blob)
        self.assertNotIn("extra_env", blob)
        self.assertEqual([item["adapter"] for item in stored["declared_adapters"]], ["my-echo"])
        self.assertEqual(stored["declared_adapters"][0]["argv"], ["--stdio"])
        self.assertEqual(set(self.app.store.get("declared_adapters", self.host_id)["rows"]), {"my-echo"})
        self.assertFalse(self.app.adapter_known(self.host_id, "legacy-private-cli"))
        self.sync_report(command, raw)
        self.assertEqual(self.app.store.get("command", command["id"])["result"], stored)

    def test_sync_discover_does_not_persist_custom_rows(self):
        opened = self.app.onboarding.request("bot-main", self.work_id, {
            "host_id": self.host_id, "action": "discover"})
        command = self.app.store.get("command", opened["command_id"])
        raw = {"inventory": [{"adapter": "codex", "binary": "codex", "version": "1",
                              "argv": ["--secret"]}],
               "declared_adapters": [{"adapter": "my-echo", **dict(ROW, extra_env={"TOKEN": "secret-value"})}]}
        self.sync_report(command, raw)
        stored = self.app.store.get("command", command["id"])["result"]
        blob = json.dumps(stored)
        self.assertNotIn("my-echo", blob)
        self.assertNotIn("secret-value", blob)
        self.assertNotIn("--secret", blob)
        self.assertNotIn("declared_adapters", stored)
        self.assertEqual(stored["inventory"], [{"adapter": "codex", "binary": "codex", "version": "1"}])
        self.assertEqual(self.app.store.find("declared_adapters"), [])
        self.sync_report(command, raw)
        self.assertEqual(self.app.store.get("command", command["id"])["result"], stored)
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.request("bot-main", self.work_id, {
                "host_id": self.host_id, "action": "configure", "adapter": "my-echo"})
        self.assertEqual(ctx.exception.code, "adapter_required")

    def test_merge_keeps_an_existing_row(self):
        path = self.device_data / "adapters.json"
        path.write_text(json.dumps({"legacy-private-cli": OTHER}), encoding="utf-8")
        path.chmod(0o644)
        setup, _holder = self.device()
        setup.declare_adapter({"adapter": "my-echo", "row": dict(ROW)})
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["legacy-private-cli"], OTHER)
        self.assertEqual(stored["my-echo"], ROW)
        self.assertEqual(ADAPTER_CATALOG["my-echo"]["argv"], ["--stdio"])
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"], self.codex_argv)
        self.forget_catalog("my-echo", "legacy-private-cli")


class DeclareAdapterFileTests(unittest.TestCase):
    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "adapters.json"
        self.addCleanup(self.tmp.cleanup)

    def test_identical_rewrite_is_skipped_and_builtin_is_unchanged(self):
        adapters_file.write_merged_row(self.path, "my-echo", dict(ROW))
        text = self.path.read_text(encoding="utf-8")
        adapters_file.write_merged_row(self.path, "my-echo", dict(ROW))
        self.assertEqual(self.path.read_text(encoding="utf-8"), text)
        self.assertEqual(self.path.stat().st_mode & 0o022, 0)
        codex = list(ADAPTER_CATALOG["codex"]["argv"])
        with self.assertRaises(Fault) as ctx:
            adapters_file.write_merged_row(self.path, "codex", dict(ROW, binary="codex"))
        self.assertEqual(ctx.exception.code, "invalid_adapter_declaration")
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"], codex)
        self.assertNotIn("codex", json.loads(self.path.read_text(encoding="utf-8")))

    def test_sixty_fifth_row_leaves_the_file_unchanged(self):
        rows = {("r%d" % i): dict(ROW, binary="bin-%d" % i) for i in range(adapters_file.MAX_ROWS)}
        self.path.write_text(json.dumps(rows), encoding="utf-8")
        self.path.chmod(0o644)
        before = self.path.read_bytes()
        with self.assertRaises(Fault) as ctx:
            adapters_file.write_merged_row(self.path, "extra-row", dict(ROW))
        self.assertEqual(ctx.exception.code, "invalid_adapters_file")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.with_name(".adapters.json.tmp").exists())
        loaded = adapters_file.load(self.path)
        self.assertEqual(len(loaded), adapters_file.MAX_ROWS)
        self.assertNotIn("extra-row", loaded)
        self.assertNotIn("extra-row", ADAPTER_CATALOG)
        self.assertNotIn("r0", ADAPTER_CATALOG)


class DeclaredMapPageTests(unittest.TestCase):
    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Service(empty_config(), Path(self.tmp.name))
        self.addCleanup(self.app.store.close)

    def test_every_host_stays_visible_past_two_hundred(self):
        for i in range(201):
            self.app.store.put("declared_adapters", {
                "id": "host-%d" % i, "rows": {"my-echo": dict(ROW)}})
        found = self.app.declared_map()
        self.assertEqual(len(found), 201)
        self.assertEqual(found["host-200"]["my-echo"]["argv"], ["--stdio"])
        self.assertTrue(self.app.adapter_known("host-200", "my-echo"))
        self.assertFalse(self.app.adapter_known("host-200", "legacy-private-cli"))


if __name__ == "__main__":
    unittest.main()
