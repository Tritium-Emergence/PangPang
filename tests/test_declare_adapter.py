"""Operator approval is what lets a connector store one custom adapter row."""
import json
import os
import tempfile
import threading
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


class LocalDeclareAdapterTests(unittest.TestCase):
    """A local host stores an approved custom row in the coordinator data directory."""

    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.cfg = empty_config()
        self.cfg["hosts"] = [{
            "id": "host-local", "name": "this-machine", "transport": "local", "os": "Linux",
            "allowed_roots": [str(self.workspace)],
        }]
        self.app = Service(self.cfg, self.data, deployment_level="L0")
        self.addCleanup(self.app.store.close)
        self.addCleanup(self.tmp.cleanup)
        self.codex_argv = list(ADAPTER_CATALOG["codex"]["argv"])
        self.work_id = self.app.send("bot-main", {"text": "declare local"}, "declare-local")["work_id"]
        self.row = {"binary": "dsk-claude", "argv": ["--stdio"], "concurrency": 1, "priority": 20,
                    "use_when": "echo a line", "secret_env": "DESK_TOKEN",
                    "extra_env": {"DESK_PROFILE": "notes"}}

    def declare_body(self, **extra):
        fields = {"host_id": "host-local", "action": "declare_adapter", "adapter": "dsk-claude", **self.row}
        fields.update(extra)
        return fields

    def stage(self, **extra):
        return self.app.onboarding.request("bot-main", self.work_id, self.declare_body(**extra))

    def test_approval_writes_the_coordinator_catalog_and_configure_can_use_it(self):
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.request("bot-main", self.work_id, {
                "host_id": "host-local", "action": "configure", "adapter": "dsk-claude"})
        self.assertEqual(ctx.exception.code, "adapter_required")
        with self.assertRaises(Fault) as ctx:
            self.app.propose_executor("bot-main", self.work_id, {
                "host_id": "host-local", "adapter": "dsk-claude"})
        self.assertEqual(ctx.exception.code, "unknown_adapter")
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.request("bot-main", self.work_id, {
                "host_id": "host-local", "action": "discover"})
        self.assertEqual(ctx.exception.code, "connector_host_required")
        self.assertFalse((self.data / "adapters.json").exists())
        self.assertNotIn("dsk-claude", ADAPTER_CATALOG)

        with self.app.store.atomic():
            denied = self.stage()
        self.assertEqual(denied["status"], "needs_approval")
        self.assertFalse((self.data / "adapters.json").exists())
        self.assertEqual(self.app.store.find("command", action="declare_adapter"), [])
        proposal = self.app.store.get("setup_request", denied["request_id"])["proposal"]
        self.assertEqual(proposal["command"], ["dsk-claude", "--stdio"])
        self.assertEqual(proposal["adapter_row"], self.row)
        self.app.onboarding.deny(denied["request_id"])
        self.assertFalse((self.data / "adapters.json").exists())
        self.assertNotIn("dsk-claude", ADAPTER_CATALOG)

        staged = self.stage()
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(staged["request_id"], {"endpoint": "https://example.com"})
        self.assertEqual(ctx.exception.code, "invalid_setup_approval")
        self.assertFalse((self.data / "adapters.json").exists())
        with self.assertRaises(Fault) as ctx:
            with self.app.store.atomic():
                self.app.onboarding.approve(staged["request_id"], {})
        self.assertTrue(ctx.exception.code.startswith("io_inside_transaction"))
        self.assertFalse((self.data / "adapters.json").exists())
        self.assertEqual(self.app.store.get("setup_request", staged["request_id"])["status"], "needs_approval")

        accepted = self.app.onboarding.approve(staged["request_id"], {})
        self.assertEqual(accepted, {"accepted": True})
        self.assertEqual(self.app.store.get("setup_request", staged["request_id"])["status"], "done")
        self.assertEqual(self.app.store.find("command", action="declare_adapter"), [])
        self.assertEqual(self.app.store.find("declared_adapters"), [])
        stored = json.loads((self.data / "adapters.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["dsk-claude"], self.row)
        self.assertEqual(adapters_file.load(self.data / "adapters.json")["dsk-claude"], self.row)
        self.assertEqual((self.data / "adapters.json").stat().st_mode & 0o022, 0)
        self.assertFalse((self.data / ".adapters.json.tmp").exists())
        self.assertEqual(ADAPTER_CATALOG["dsk-claude"]["argv"], ["--stdio"])
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"], self.codex_argv)
        self.assertTrue(self.app.adapter_known("host-local", "dsk-claude"))
        names = [item["adapter"] for item in self.app.list_executors()["adapters"]]
        self.assertIn("dsk-claude", names)
        self.assertNotIn("argv", self.app.list_executors()["adapters"][0])
        told = [item for item in self.app.store.find("message") if item.get("kind") == "setup_result"]
        self.assertEqual(told[-1]["content"]["declared_adapters"][0]["binary"], "dsk-claude")
        self.assertEqual(told[-1]["content"]["declared_adapters"][0]["argv"], ["--stdio"])

        missing = self.app.propose_executor("bot-main", self.work_id, {
            "host_id": "host-local", "adapter": "dsk-claude", "profile": "missing"})
        with patch("pangpang.onboarding.shutil.which", return_value=None):
            with self.assertRaises(Fault) as ctx:
                self.app.onboarding.approve(missing["staged"]["request_id"], {})
        self.assertEqual(ctx.exception.code, "cli_not_installed")
        self.assertNotIn("dsk-claude-missing-host-local", self.app.execution.installations)

        proposed = self.app.propose_executor("bot-main", self.work_id, {
            "host_id": "host-local", "adapter": "dsk-claude", "priority": 4, "use_when": "notes only"})
        self.assertEqual(proposed["staged"]["status"], "needs_approval")
        card = self.app.store.get("setup_request", proposed["staged"]["request_id"])
        self.assertEqual(card["proposal"]["action"], "executor_config")
        self.assertEqual(card["proposal"]["command"], ["dsk-claude", "--stdio"])
        card["proposal"]["command"] = ["evil-binary", "--not-catalog"]
        self.app.store.put("setup_request", card)
        with patch("pangpang.onboarding.shutil.which", return_value="/usr/local/bin/dsk-claude"):
            created = self.app.onboarding.approve(card["id"], {})
        iid = "dsk-claude-host-local"
        self.assertEqual(created["installation_id"], iid)
        self.assertFalse(created["probe"]["available"])
        self.assertEqual(created["probe"]["reason"], "binary_not_found")
        installed = self.app.execution.installations[iid]
        self.assertEqual(installed["argv"], ["/usr/local/bin/dsk-claude", "--stdio"])
        self.assertEqual(installed["cwd"], str(self.workspace.resolve()))
        self.assertEqual(installed["extra_env"], {"DESK_PROFILE": "notes"})
        self.assertEqual(installed["priority"], 4)
        self.assertEqual(installed["use_when"], "notes only")
        self.assertEqual(self.app._route_priority(installed), 4)
        self.assertEqual(self.app._use_when(installed), "notes only")
        self.assertNotIn(iid, self.app.execution.unknown_adapters)
        blocked = self.app.route("bot-main", {"id": self.work_id}, {"installation_id": "dsk-claude"})
        self.assertIsNone(blocked["selected"])
        reasons = {item["reason"] for item in blocked["rejected"] if item["installation_id"] == iid}
        self.assertIn("discovery_failed", reasons)
        self.assertNotIn("unknown_adapter", reasons)
        self.app.store.put("probe", {"id": iid, "available": True, "supervision": "windows-job-object",
                                     "at": 1})
        routed = self.app.route("bot-main", {"id": self.work_id}, {"installation_id": "dsk-claude"})
        self.assertEqual(routed["selected"]["installation_id"], iid)
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"], self.codex_argv)

    def test_declare_rejects_a_path_binary_before_writing(self):
        before = self.app.store.find("setup_request")
        for binary in ("~/.local/bin/dsk-claude", "/usr/local/bin/dsk-claude", "bin/dsk-claude",
                       ".\\dsk-claude"):
            with self.subTest(binary=binary):
                with self.assertRaises(Fault) as ctx:
                    self.stage(binary=binary)
                self.assertEqual(ctx.exception.code, "invalid_adapter_declaration")
                self.assertIn("PATH basename", ctx.exception.advice)
                self.assertIn("dsk-claude", ctx.exception.advice)
        self.assertEqual(self.app.store.find("setup_request"), before)
        self.assertFalse((self.data / "adapters.json").exists())
        self.assertNotIn("dsk-claude", ADAPTER_CATALOG)

    def _declare_stored_row(self):
        accepted = self.app.onboarding.approve(self.stage()["request_id"], {})
        self.assertEqual(accepted, {"accepted": True})

    def test_configure_resolves_a_stored_path_after_expanduser(self):
        self._declare_stored_row()
        dirty = "~/.local/bin/dsk-claude"
        ADAPTER_CATALOG["dsk-claude"] = dict(ADAPTER_CATALOG["dsk-claude"], binary=dirty)
        expanded = os.path.expanduser(dirty)
        self.assertNotEqual(expanded, dirty)
        self.assertNotIn("~", expanded)

        def which_expanded(candidate):
            return expanded if candidate == expanded else None

        proposed = self.app.propose_executor("bot-main", self.work_id, {
            "host_id": "host-local", "adapter": "dsk-claude", "profile": "expanded"})
        with patch("pangpang.onboarding.shutil.which", side_effect=which_expanded) as mocked:
            created = self.app.onboarding.approve(proposed["staged"]["request_id"], {})
        self.assertEqual(mocked.call_args_list[0].args[0], expanded)
        iid = "dsk-claude-expanded-host-local"
        self.assertEqual(created["installation_id"], iid)
        self.assertEqual(self.app.execution.installations[iid]["argv"][0], expanded)

    def test_configure_uses_basename_or_reports_a_missing_stored_path(self):
        self._declare_stored_row()
        dirty = "~/.local/bin/dsk-claude"
        ADAPTER_CATALOG["dsk-claude"] = dict(ADAPTER_CATALOG["dsk-claude"], binary=dirty)
        expanded = os.path.expanduser(dirty)
        missing = self.app.propose_executor("bot-main", self.work_id, {
            "host_id": "host-local", "adapter": "dsk-claude", "profile": "absent"})
        with patch("pangpang.onboarding.shutil.which", return_value=None):
            with self.assertRaises(Fault) as ctx:
                self.app.onboarding.approve(missing["staged"]["request_id"], {})
        self.assertEqual(ctx.exception.code, "cli_not_installed")
        self.assertIn(dirty, ctx.exception.advice)
        self.assertIn("PATH", ctx.exception.advice)
        self.assertNotIn("dsk-claude-absent-host-local", self.app.execution.installations)

        seen = []

        def which_basename(candidate):
            seen.append(candidate)
            return "/usr/local/bin/dsk-claude" if candidate == "dsk-claude" else None

        proposed = self.app.propose_executor("bot-main", self.work_id, {
            "host_id": "host-local", "adapter": "dsk-claude", "profile": "basename"})
        with patch("pangpang.onboarding.shutil.which", side_effect=which_basename):
            created = self.app.onboarding.approve(proposed["staged"]["request_id"], {})
        self.assertEqual(seen, [expanded, "dsk-claude"])
        iid = "dsk-claude-basename-host-local"
        self.assertEqual(created["installation_id"], iid)
        self.assertEqual(self.app.execution.installations[iid]["argv"][0], "/usr/local/bin/dsk-claude")

    def test_builtin_protocol_and_conflict_are_refused(self):
        before = self.app.store.find("setup_request")
        with self.assertRaises(Fault) as ctx:
            self.stage(adapter="codex")
        self.assertEqual(ctx.exception.code, "invalid_adapter_declaration")
        self.assertIn("built-in", ctx.exception.advice)
        with self.assertRaises(Fault) as ctx:
            self.stage(stdin_protocol="claude-stream-json")
        self.assertEqual(ctx.exception.code, "invalid_adapter_declaration")
        self.assertIn("stdin_protocol", ctx.exception.advice)
        self.assertEqual(self.app.store.find("setup_request"), before)
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"], self.codex_argv)
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.request("bot-main", self.work_id, self.declare_body(host_id="missing"))
        self.assertEqual(ctx.exception.code, "connector_host_required")

        staged = self.stage()
        path = self.data / "adapters.json"
        other = {"binary": "dsk-claude", "argv": ["--old"], "concurrency": 1, "priority": 20,
                 "use_when": "echo a line"}
        path.write_text(json.dumps({"dsk-claude": other, "my-echo": dict(ROW)}), encoding="utf-8")
        path.chmod(0o644)
        before_text = path.read_text(encoding="utf-8")
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(staged["request_id"], {})
        self.assertEqual(ctx.exception.code, "adapter_declaration_conflict")
        self.assertEqual(path.read_text(encoding="utf-8"), before_text)
        self.assertEqual(self.app.store.get("setup_request", staged["request_id"])["status"], "needs_approval")
        self.assertNotIn("dsk-claude", ADAPTER_CATALOG)

        fresh = self.stage()
        stored_req = self.app.store.get("setup_request", fresh["request_id"])
        stored_req["proposal"]["command"] = ["dsk-claude", "--swapped"]
        self.app.store.put("setup_request", stored_req)
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(fresh["request_id"], {})
        self.assertEqual(ctx.exception.code, "adapter_declaration_mismatch")
        self.assertEqual(path.read_text(encoding="utf-8"), before_text)

        opened = self.app.onboarding.request("bot-main", self.work_id, {
            "host_id": "host-local", "action": "configure", "adapter": "codex"})
        self.assertEqual(opened["status"], "needs_approval")
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(opened["request_id"], {})
        self.assertEqual(ctx.exception.code, "cli_not_installed")
        self.assertNotIn("codex-host-local", self.app.execution.installations)

    def test_deny_and_approve_cannot_both_commit(self):
        denied = self.stage()
        self.assertEqual(self.app.onboarding.deny(denied["request_id"]), {"denied": True})
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(denied["request_id"], {})
        self.assertEqual(ctx.exception.code, "setup_request_expired_or_used")
        self.assertEqual(self.app.store.get("setup_request", denied["request_id"])["status"], "denied")
        self.assertFalse((self.data / "adapters.json").exists())
        self.assertFalse(self.app.adapter_known("host-local", "dsk-claude"))

        staged = self.stage()
        identity = staged["request_id"]
        at_write = threading.Event()
        deny_started = threading.Event()
        real_write = adapters_file.write_merged_row

        def paused(path, name, row):
            at_write.set()
            self.assertTrue(deny_started.wait(3), "deny did not reach the claimed card")
            return real_write(path, name, row)

        results = {}

        def approve():
            try:
                results["approve"] = self.app.onboarding.approve(identity, {})
            except Exception as exc:
                results["approve_error"] = exc

        def deny():
            deny_started.set()
            try:
                results["deny"] = self.app.onboarding.deny(identity)
            except Exception as exc:
                results["deny_error"] = exc

        with patch("pangpang.adapters_file.write_merged_row", side_effect=paused):
            worker = threading.Thread(target=approve)
            worker.start()
            self.assertTrue(at_write.wait(3), "approve did not reach the file write")
            other = threading.Thread(target=deny)
            other.start()
            worker.join(5)
            other.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(other.is_alive())
        self.assertEqual(results.get("approve"), {"accepted": True})
        self.assertNotIn("deny", results)
        self.assertIsInstance(results.get("deny_error"), Fault)
        self.assertEqual(results["deny_error"].code, "setup_request_expired_or_used")
        self.assertEqual(self.app.store.get("setup_request", identity)["status"], "done")
        stored = json.loads((self.data / "adapters.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["dsk-claude"], self.row)
        self.assertTrue(self.app.adapter_known("host-local", "dsk-claude"))

    def test_concurrent_approvals_keep_both_rows_after_reload(self):
        first = self.stage()
        second_row = dict(ROW)
        second = self.app.onboarding.request("bot-main", self.work_id, {
            "host_id": "host-local", "action": "declare_adapter", "adapter": "my-echo", **second_row})
        barrier = threading.Barrier(2)
        peer_in_load = threading.Event()
        real_load = adapters_file.load
        leader = {"id": None}
        leader_mu = threading.Lock()
        target = self.data / "adapters.json"

        def load(path):
            rows = real_load(path)
            if Path(path) != target:
                return rows
            with leader_mu:
                lead = leader["id"] is None
                if lead:
                    leader["id"] = threading.get_ident()
            if lead:
                peer_in_load.wait(1)
            else:
                peer_in_load.set()
            return rows

        boxes = ([], [])

        def run(identity, box):
            try:
                barrier.wait(3)
                box.append(self.app.onboarding.approve(identity, {}))
            except Exception as exc:
                box.append(exc)

        threads = [threading.Thread(target=run, args=(item["request_id"], box))
                   for item, box in ((first, boxes[0]), (second, boxes[1]))]
        with patch("pangpang.adapters_file.load", side_effect=load):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
        for thread in threads:
            self.assertFalse(thread.is_alive())
        self.assertEqual(boxes[0], [{"accepted": True}])
        self.assertEqual(boxes[1], [{"accepted": True}])
        disk = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(disk["dsk-claude"], self.row)
        self.assertEqual(disk["my-echo"], second_row)
        for name in ("dsk-claude", "my-echo"):
            ADAPTER_CATALOG.pop(name, None)
            adapters_file._merged_names.discard(name)
        adapters_file.merge(target)
        self.assertEqual(ADAPTER_CATALOG["dsk-claude"], self.row)
        self.assertEqual(ADAPTER_CATALOG["my-echo"], second_row)
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"], self.codex_argv)

    def test_local_configure_keeps_the_requested_cwd(self):
        staged = self.stage()
        self.assertEqual(self.app.onboarding.approve(staged["request_id"], {}), {"accepted": True})
        project = self.workspace / "project"
        project.mkdir()
        opened = self.app.onboarding.request("bot-main", self.work_id, {
            "host_id": "host-local", "action": "configure", "adapter": "dsk-claude",
            "cwd": str(project)})
        card = self.app.store.get("setup_request", opened["request_id"])
        self.assertEqual(card["proposal"]["action"], "executor_config")
        self.assertEqual(card["proposal"]["draft"]["cwd"], str(project))
        with patch("pangpang.onboarding.shutil.which", return_value="/usr/local/bin/dsk-claude"):
            created = self.app.onboarding.approve(opened["request_id"], {})
        installed = self.app.execution.installations[created["installation_id"]]
        self.assertEqual(installed["cwd"], str(project.resolve()))


if __name__ == "__main__":
    unittest.main()
