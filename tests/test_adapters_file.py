import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_bot_runtime import config
from test_unknown_adapter_startup import _removed_adapter_name
from pangpang import adapters_file
from pangpang.connector import Connector, policy_digest
from pangpang.execution import ADAPTER_CATALOG, Execution
from pangpang.local_setup import LocalSetup
from pangpang.service import Service
from pangpang.store import Fault, Store

ROW = {"binary": "opencode", "argv": ["run"], "concurrency": 2, "priority": 15,
       "use_when": "自定义执行器测试行。"}


def catalog_teardown(case):
    def clean():
        for name in list(adapters_file._merged_names):
            ADAPTER_CATALOG.pop(name, None)
        adapters_file._merged_names.clear()
    case.addCleanup(clean)


class AdaptersFileUnitTests(unittest.TestCase):
    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, document, name="adapters.json", mode=0o644):
        path = self.root / name
        text = document if isinstance(document, str) else json.dumps(document, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def test_valid_rows_merge_and_remerge_is_idempotent(self):
        path = self.write({"opencode-test": ROW})
        self.assertEqual(adapters_file.merge(path), ["opencode-test"])
        self.assertEqual(ADAPTER_CATALOG["opencode-test"], ROW)
        self.assertEqual(adapters_file.merge(path), ["opencode-test"])
        self.assertEqual(ADAPTER_CATALOG["opencode-test"], ROW)

    def test_absent_file_is_noop(self):
        self.assertEqual(adapters_file.merge(self.root / "missing.json"), [])

    def test_bad_json_reports_path_and_line(self):
        path = self.write('{"opencode-test": %s,\n  "second": {broken}}' % json.dumps(ROW))
        with self.assertRaises(Fault) as ctx:
            adapters_file.merge(path)
        self.assertEqual(ctx.exception.code, "invalid_adapters_file")
        self.assertIn(str(path), ctx.exception.advice)
        self.assertIn("line 2", ctx.exception.advice)

    def test_missing_required_field_names_field(self):
        row = dict(ROW)
        del row["priority"]
        with self.assertRaises(Fault) as ctx:
            adapters_file.merge(self.write({"opencode-test": row}))
        self.assertIn("'priority' is required", ctx.exception.advice)
        self.assertIn("opencode-test", ctx.exception.advice)

    def test_trust_and_protocol_keys_are_rejected(self):
        for key in ("pinned", "sha256", "guard_sha256", "interjection_tier", "stdin_protocol",
                    "session_protocol", "resume_protocol", "permission_bridge", "models_json"):
            with self.subTest(key=key):
                with self.assertRaises(Fault) as ctx:
                    adapters_file.merge(self.write({"opencode-test": dict(ROW, **{key: "x"})},
                                                   name="row-%s.json" % key))
                self.assertIn(key, ctx.exception.advice)
                self.assertNotIn("opencode-test", ADAPTER_CATALOG)

    def test_builtin_collision_reports_line(self):
        path = self.write('{\n  "first-test": %s,\n  "codex": %s\n}' % (json.dumps(ROW), json.dumps(ROW)))
        with self.assertRaises(Fault) as ctx:
            adapters_file.merge(path)
        self.assertIn("built-in", ctx.exception.advice)
        self.assertIn("line 3", ctx.exception.advice)

    @unittest.skipUnless(hasattr(os, "geteuid"), "POSIX permission semantics")
    def test_loose_file_permissions_rejected(self):
        with self.assertRaises(Fault) as ctx:
            adapters_file.merge(self.write({"opencode-test": ROW}, mode=0o666))
        self.assertIn("writable", ctx.exception.advice)

    def test_row_count_cap(self):
        rows = {("custom-%02d" % i): ROW for i in range(adapters_file.MAX_ROWS + 1)}
        with self.assertRaises(Fault) as ctx:
            adapters_file.merge(self.write(rows))
        self.assertIn("at most", ctx.exception.advice)

    def test_argv_and_concurrency_shape_rejected(self):
        for field, value in (("argv", "run"), ("argv", ["ok", 5]), ("concurrency", True),
                             ("concurrency", 0), ("priority", "high"), ("binary", ""),
                             ("use_when", "  ")):
            with self.subTest(field=field, value=value):
                with self.assertRaises(Fault):
                    adapters_file.merge(self.write({"opencode-test": {**ROW, field: value}},
                                                   name="shape.json"))

    def test_extra_env_rules(self):
        good = self.write({"opencode-test": dict(ROW, extra_env={"OPENAI_BASE_URL": "http://127.0.0.1:1"},
                                                 secret_env="OPENAI_API_KEY")})
        self.assertEqual(adapters_file.merge(good), ["opencode-test"])
        self.assertEqual(ADAPTER_CATALOG["opencode-test"]["extra_env"], {"OPENAI_BASE_URL": "http://127.0.0.1:1"})
        for bad_env in ({"lower_key": "v"}, {"OK": 5}, {"OK": "with\x00nul"}):
            with self.subTest(env=bad_env):
                with self.assertRaises(Fault):
                    adapters_file.merge(self.write({"opencode-test": dict(ROW, extra_env=bad_env)},
                                                   name="env.json"))

    def test_removed_private_default_can_be_registered_from_the_file(self):
        name = _removed_adapter_name()
        self.assertNotIn(name, ADAPTER_CATALOG)
        row = dict(ROW, binary=name, argv=["-p"], concurrency=2, priority=10,
                   use_when="迁移后的私人执行器。")
        path = self.write({name: row})
        self.assertEqual(adapters_file.merge(path), [name])
        self.assertEqual(ADAPTER_CATALOG[name]["argv"], ["-p"])
        self.assertEqual(ADAPTER_CATALOG[name]["use_when"], "迁移后的私人执行器。")

    def test_conflicting_second_file_refuses(self):
        self.write({"opencode-test": ROW}, name="a.json")
        adapters_file.merge(self.root / "a.json")
        with self.assertRaises(Fault) as ctx:
            adapters_file.merge(self.write({"opencode-test": dict(ROW, priority=99)}, name="b.json"))
        self.assertIn("conflicts", ctx.exception.advice)

    def test_binary_must_be_a_single_path_basename(self):
        path = self.write({"opencode-test": dict(ROW, binary="dsk-claude")})
        self.assertEqual(adapters_file.merge(path), ["opencode-test"])
        self.assertEqual(ADAPTER_CATALOG["opencode-test"]["binary"], "dsk-claude")
        accepted = self.write({"opencode-test": dict(ROW, binary="dsk-claude.exe")}, name="exe.json")
        self.assertEqual(adapters_file.load(accepted)["opencode-test"]["binary"], "dsk-claude.exe")
        for binary in ("~/.local/bin/dsk-claude", "/usr/local/bin/dsk-claude", "bin/dsk-claude",
                       ".\\dsk-claude", "dsk~claude", ".", "..", "C:dsk-claude"):
            with self.subTest(binary=binary):
                with self.assertRaises(Fault) as ctx:
                    adapters_file.load(self.write({"opencode-test": {**ROW, "binary": binary}},
                                                  name="path.json"))
                self.assertEqual(ctx.exception.code, "invalid_adapters_file")
                self.assertIn("binary", ctx.exception.advice)
                self.assertIn("PATH basename", ctx.exception.advice)
                self.assertEqual(ADAPTER_CATALOG["opencode-test"]["binary"], "dsk-claude")

    def test_write_rejects_a_path_binary_and_leaves_the_file(self):
        path = self.write({"opencode-test": ROW})
        before = path.read_text(encoding="utf-8")
        with self.assertRaises(Fault) as ctx:
            adapters_file.write_merged_row(path, "other-cli",
                                           {**ROW, "binary": "~/.local/bin/dsk-claude"})
        self.assertEqual(ctx.exception.code, "invalid_adapter_declaration")
        self.assertIn("PATH basename", ctx.exception.advice)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertFalse(path.with_name(".adapters.json.tmp").exists())
        self.assertNotIn("other-cli", ADAPTER_CATALOG)


class CustomAdapterStartupTests(unittest.TestCase):
    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.data = Path(self.tmp.name) / "data"
        self.addCleanup(self.tmp.cleanup)

    def cfg(self):
        cfg = config(self.root)
        cfg["installations"][0]["adapter"] = "opencode-test"
        return cfg

    def test_custom_installation_without_file_refuses_startup(self):
        with self.assertRaises(Fault) as ctx:
            Service(self.cfg(), self.data)
        self.assertEqual(ctx.exception.code, "invalid_adapter")
        self.assertIn("adapters.json", ctx.exception.advice)

    def test_custom_installation_with_file_starts(self):
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "adapters.json").write_text(json.dumps({"opencode-test": ROW}), encoding="utf-8")
        (self.data / "adapters.json").chmod(0o644)
        app = Service(self.cfg(), self.data)
        self.addCleanup(app.store.close)
        self.assertEqual(app.execution.installations["codex-local"]["adapter"], "opencode-test")


class CustomAdapterDeviceTests(unittest.TestCase):
    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.data = self.root / "device"
        self.data.mkdir()
        self.store = Store(self.data / "connector.db")
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)
        adapters_file.merge(self.write_adapters())
        live = {"host_id": "local",
                "hosts": [{"id": "local", "transport": "local", "os": "Linux",
                           "allowed_roots": [str(self.workspace)]}],
                "installations": [], "resources": []}
        self.holder = SimpleNamespace(store=self.store, data=self.data, config=live,
                                      config_revision=0, execution=Execution(live, self.data),
                                      host_id="local")

    def write_adapters(self):
        path = self.data / "adapters.json"
        path.write_text(json.dumps({"opencode-test": ROW}), encoding="utf-8")
        path.chmod(0o644)
        return path

    def test_discover_and_configure_custom_adapter(self):
        def fake_which(binary):
            return "/usr/local/bin/opencode" if binary == "opencode" else None
        with patch("pangpang.local_setup.shutil.which", side_effect=fake_which), \
                patch("pangpang.local_setup.probe", return_value={"version": "1.2.3"}):
            setup = LocalSetup(self.holder)
            inventory = setup.discover()["inventory"]
            self.assertEqual([x["adapter"] for x in inventory], ["opencode-test"])
            self.assertEqual(inventory[0]["binary"], "/usr/local/bin/opencode")
            info = setup.configure({"adapter": "opencode-test"})
        installed = self.holder.execution.installations["opencode-test-local"]
        self.assertEqual(installed["adapter"], "opencode-test")
        self.assertEqual(installed["argv"], ["/usr/local/bin/opencode", "run"])
        self.assertEqual(installed["max_concurrency"], ROW["concurrency"])
        self.assertEqual(info["installation"]["adapter"], "opencode-test")


class CustomAdapterEndToEndTests(unittest.TestCase):
    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "workspace"
        self.root.mkdir()
        device_data = Path(self.tmp.name) / "device"
        cloud_data = Path(self.tmp.name) / "cloud"
        for data in (device_data, cloud_data):
            data.mkdir()
            path = data / "adapters.json"
            path.write_text(json.dumps({"opencode-test": ROW}), encoding="utf-8")
            path.chmod(0o644)
        local = config(self.root)
        local["installations"][0].update(adapter="opencode-test", enabled=True)
        local.update(host_id="local", cloud_url="http://127.0.0.1:1",
                     credential_env="TEST_DEVICE_KEY")
        self.connector = Connector(local, device_data)
        cloud = config(self.root)
        cloud["hosts"][0].update(transport="connector", credential_env="TEST_DEVICE_KEY")
        cloud["installations"][0]["adapter"] = "opencode-test"
        self.app = Service(cloud, cloud_data)
        self.connector.request = lambda body: self.app.broker.sync("local", body)
        self.connector.tick()
        probe = self.connector.execution.probe("codex-local")
        probe.update(id="codex-local", at=time.time(),
                     policy_digest=policy_digest(self.connector.config, "codex-local"))
        self.app.store.put("probe", probe)

    def tearDown(self):
        for run in self.connector.store.find("local_run", status="active"):
            try:
                self.connector.execution.cancel(run["task"])
            except Exception:
                pass
        self.connector.close()
        self.app.store.close()
        self.tmp.cleanup()

    def start_payload(self):
        m = self.app.send("bot-main", {"text": "制作结果", "allow_write": True}, "custom-work")
        r = self.app.submit_task("bot-main", m["work_id"],
                                 {"prompt": "中文内容", "outputs": ["result.txt"]})
        self.app.task_step()
        task = self.app.store.get("task", r["task_id"])
        command = [c for c in self.app.store.find("command", action="start", limit=1000)
                   if c.get("payload", {}).get("execution_id") == task["execution_id"]][0]
        return task, {k: command[k] for k in ("id", "action", "payload", "expires")}

    def test_dual_placed_catalog_executes_to_receipt(self):
        task, payload = self.start_payload()
        self.connector.execute(payload)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.connector.tick()
            current = self.app.store.get("task", task["id"])
            if current["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(.05)
        self.assertEqual(current["status"], "succeeded", current)
        self.assertTrue((self.root / "result.txt").exists())

    def test_same_name_different_argv_device_refuses_start(self):
        task, payload = self.start_payload()
        self.connector.config["installations"][0]["argv"] = ["/drifted/opencode", "run"]
        result = self.connector.start(payload["payload"])
        self.assertEqual(result.get("rejected"), True)
        run = self.connector.store.get("local_run", task["execution_id"])
        self.assertEqual(run["receipt"]["error"], "local_policy_changed_probe_and_resubmit")


if __name__ == "__main__":
    unittest.main()
