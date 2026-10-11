import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_bot_runtime import config
from pangpang.execution import Execution, validate_extra_argv
from pangpang.local_setup import LocalSetup
from pangpang.onboarding import empty_config
from pangpang.service import Service
from pangpang.store import Fault, Store, uid
from pangpang.vault import Vault


class ValidateExtraArgvTests(unittest.TestCase):
    def test_valid_rows(self):
        validate_extra_argv("qwen-claude", ["--max-turns", "10"])
        validate_extra_argv("codex", ["--strict-config", "-c", "model=qwen"])
        validate_extra_argv("codex", [])
        validate_extra_argv(None, ["--anything"])
        validate_extra_argv("no-such-adapter", ["--anything"])

    def test_shape_rejections(self):
        bad = ["--x", {"a": 1}, 5, ["--ok", 5], [""], ["a" * 1025], ["\x00x"],
               ["--x"] * 17, ["a" * 1024] * 5, ("--tuple",)]
        for value in bad:
            with self.subTest(value=str(value)[:40]):
                with self.assertRaises(Fault) as ctx:
                    validate_extra_argv("qwen-claude", value)
                self.assertEqual(ctx.exception.code, "invalid_extra_argv")

    def test_permission_disabling_flags_rejected(self):
        for item in ("--yolo", "--YOLO", "--dangerously-skip-permissions", "--no-sandbox",
                     "--full-auto", "--approve-for-me", "--bypass-sandbox", "--skip-permissions"):
            with self.subTest(item=item):
                with self.assertRaises(Fault) as ctx:
                    validate_extra_argv("qwen-claude", [item])
                self.assertEqual(ctx.exception.code, "invalid_extra_argv")

    def test_codex_exec_only_flags_rejected_not_dropped(self):
        for item in ("--skip-git-repo-check", "--json", "--color", "--profile",
                     "-p", "--add-dir", "-i"):
            with self.subTest(item=item):
                with self.assertRaises(Fault) as ctx:
                    validate_extra_argv("codex", [item, "v"])
                self.assertEqual(ctx.exception.code, "invalid_extra_argv")
                self.assertIn("exec-only", ctx.exception.advice)
        validate_extra_argv("codex", ["--enable", "feature", "--strict-config"])
        validate_extra_argv("qwen-claude", ["--skip-git-repo-check"])

    def test_codex_sandbox_bypass_and_listen_rejected(self):
        for items in (["--sandbox", "danger-full-access"], ["--sandbox=danger-full-access"],
                      ["--listen", "http://127.0.0.1:9999"], ["--listen=stdio://"]):
            with self.subTest(items=items):
                with self.assertRaises(Fault) as ctx:
                    validate_extra_argv("codex", items)
                self.assertEqual(ctx.exception.code, "invalid_extra_argv")
        validate_extra_argv("codex", ["--sandbox", "workspace-write", "-m", "gpt"])

    def test_codex_bare_tokens_rejected_not_repositioned(self):
        for items in (["--foo", "bar"], ["login"], ["--strict-config", "app"]):
            with self.subTest(items=items):
                with self.assertRaises(Fault) as ctx:
                    validate_extra_argv("codex", items)
                self.assertEqual(ctx.exception.code, "invalid_extra_argv")
                self.assertIn("bare token", ctx.exception.advice)
        validate_extra_argv("codex", ["-c", "model_reasoning_effort=high"])
        validate_extra_argv("codex", ["--enable", "feature", "--config", "a=b"])
        validate_extra_argv("qwen-claude", ["login"])

    def test_codex_dropped_tokens_rejected_not_silent(self):
        for items in (["-"], ["--foo={prompt}"], ["{prompt}"], ["-", "--strict-config"]):
            with self.subTest(items=items):
                with self.assertRaises(Fault) as ctx:
                    validate_extra_argv("codex", items)
                self.assertEqual(ctx.exception.code, "invalid_extra_argv")
                self.assertIn("dropped", ctx.exception.advice)
        validate_extra_argv("codex", ["-c", "model={prompt}"])
        validate_extra_argv("qwen-claude", ["-", "--user={prompt}"])


class ExecutionExtraArgvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "workspace").mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def cfg(self, installation_extra):
        cfg = config(self.root / "workspace")
        cfg["installations"][0].update(installation_extra)
        return cfg

    def task(self):
        return {"installation_id": "codex-local", "execution_id": "exec-1", "spec_digest": "d",
                "prompt": "x", "inputs": []}

    def test_execution_validates_installation_extra_argv(self):
        Execution(self.cfg({"extra_argv": ["--strict-config"]}), self.root / "data")
        for bad in (["--yolo"], "--not-a-list", ["--skip-git-repo-check"]):
            with self.subTest(extra_argv=str(bad)[:30]):
                with self.assertRaises(Fault) as ctx:
                    Execution(self.cfg({"extra_argv": bad}), self.root / "data")
                self.assertEqual(ctx.exception.code, "invalid_extra_argv")

    def test_spec_appends_extra_argv_after_catalog_argv(self):
        base = list(config(self.root / "workspace")["installations"][0]["argv"])
        execution = Execution(self.cfg({"extra_argv": ["--flag=v", "--strict-config"]}), self.root / "data")
        self.assertEqual(execution.spec(self.task())["argv"], base + ["--flag=v", "--strict-config"])

    def test_spec_without_extra_argv_is_unchanged(self):
        base = list(config(self.root / "workspace")["installations"][0]["argv"])
        execution = Execution(self.cfg({}), self.root / "data")
        self.assertEqual(execution.spec(self.task())["argv"], base)


class DeviceConfigureExtraArgvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.data = self.root / "device"
        self.data.mkdir()
        self.store = Store(self.data / "connector.db")
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)
        live = {"host_id": "local",
                "hosts": [{"id": "local", "transport": "local", "os": "Linux",
                           "allowed_roots": [str(self.workspace)]}],
                "installations": [], "resources": []}
        self.holder = SimpleNamespace(store=self.store, data=self.data, config=live,
                                      config_revision=0, execution=Execution(live, self.data),
                                      host_id="local")
        self.setup = LocalSetup(self.holder)

    def configure(self, body):
        with patch.object(LocalSetup, "discover",
                          return_value={"inventory": [{"adapter": "codex", "binary": "/usr/bin/codex"}]}):
            return self.setup.configure(body)

    def test_extra_argv_persisted_kept_and_cleared(self):
        self.configure({"adapter": "codex", "extra_argv": ["--strict-config"]})
        installed = self.holder.execution.installations["codex-local"]
        self.assertEqual(installed["extra_argv"], ["--strict-config"])
        self.configure({"adapter": "codex"})
        self.assertEqual(self.holder.execution.installations["codex-local"]["extra_argv"],
                         ["--strict-config"])
        self.configure({"adapter": "codex", "extra_argv": []})
        self.assertNotIn("extra_argv", self.holder.execution.installations["codex-local"])

    def test_bad_extra_argv_rejected_before_write(self):
        for body in ({"adapter": "codex", "extra_argv": ["--yolo"]},
                     {"adapter": "codex", "extra_argv": "--not-a-list"},
                     {"adapter": "codex", "extra_argv": ["--skip-git-repo-check"]}):
            with self.subTest(body=body):
                with self.assertRaises(Fault):
                    self.configure(body)
        self.assertEqual(self.holder.execution.installations, {})


class OnboardingExtraArgvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.app = Service(empty_config(), self.root.parent / "cloud")
        self.addCleanup(self.app.store.close)
        self.addCleanup(self.tmp.cleanup)

    def paired_host(self):
        data = self.root.parent / "device"
        data.mkdir(exist_ok=True)
        store = Store(data / "connector.db")
        identity = uid("device")
        store.put("identity", {"id": "device", "instance_id": identity})
        store.close()
        vault = Vault(data / "vault")
        pub = vault.public_key()
        vault.close()
        code = self.app.onboarding.pairing_code()["code"]
        pair = self.app.onboarding.pair({"code": code, "instance_id": identity, "public_key": pub,
                                         "name": "PC", "os": "Linux"})
        return pair["host_id"]

    def request(self, host_id, body):
        work = self.app.send("bot-main", {"text": "connect"}, "argv-req")
        return self.app.onboarding.request("bot-main", work["work_id"], dict(body, host_id=host_id))

    def command_for(self, request_id):
        return [c for c in self.app.store.find("command", action="configure")
                if c["payload"].get("request_id") == request_id][0]

    def test_model_cannot_propose_extra_argv(self):
        host_id = self.paired_host()
        with self.assertRaises(Fault) as ctx:
            self.request(host_id, {"action": "configure", "adapter": "codex",
                                   "extra_argv": ["--strict-config"]})
        self.assertEqual(ctx.exception.code, "operator_owned_setup_field")

    def test_approve_extra_argv_lands_in_device_command(self):
        host_id = self.paired_host()
        req = self.request(host_id, {"action": "configure", "adapter": "codex"})
        self.app.onboarding.approve(req["request_id"], {"extra_argv": ["--strict-config"]})
        self.assertEqual(self.command_for(req["request_id"])["payload"]["extra_argv"],
                         ["--strict-config"])

    def test_approve_rejects_bad_extra_argv(self):
        host_id = self.paired_host()
        req = self.request(host_id, {"action": "configure", "adapter": "codex"})
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(req["request_id"], {"extra_argv": ["--yolo"]})
        self.assertEqual(ctx.exception.code, "invalid_extra_argv")
        req2 = self.request(host_id, {"action": "configure", "adapter": "codex"})
        with self.assertRaises(Fault):
            self.app.onboarding.approve(req2["request_id"], {"extra_argv": "--not-a-list"})


class ConfigEndpointExtraArgvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.app = Service(config(self.root), self.root.parent / "data")
        self.addCleanup(self.app.store.close)
        self.addCleanup(self.tmp.cleanup)

    def test_set_and_clear_extra_argv(self):
        self.app.configure_installation("codex-local", {"extra_argv": ["--strict-config"]})
        self.assertEqual(self.app.execution.installations["codex-local"]["extra_argv"],
                         ["--strict-config"])
        saved = self.app.store.get("settings", "managed")["config"]
        inst = next(i for i in saved["installations"] if i["id"] == "codex-local")
        self.assertEqual(inst["extra_argv"], ["--strict-config"])
        self.app.configure_installation("codex-local", {"extra_argv": []})
        self.assertNotIn("extra_argv", self.app.execution.installations["codex-local"])

    def test_rejected_values_never_land(self):
        for bad in (["--yolo"], "--not-a-list", ["--skip-git-repo-check"], ["--x"] * 17):
            with self.subTest(extra_argv=str(bad)[:30]):
                with self.assertRaises(Fault) as ctx:
                    self.app.configure_installation("codex-local", {"extra_argv": bad})
                self.assertEqual(ctx.exception.code, "invalid_extra_argv")
        self.assertNotIn("extra_argv", self.app.execution.installations["codex-local"])


class ExtraArgvReachesExecutorTests(unittest.TestCase):
    def test_final_argv_contains_extra_argv(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve() / "workspace"
        root.mkdir()
        cfg = config(root)
        cfg["installations"][0]["argv"] = [
            sys.executable, "-c",
            "import pathlib,sys; pathlib.Path('result.txt').write_text(repr(sys.argv))"]
        cfg["installations"][0]["extra_argv"] = ["--strict-config", "-c", "sandbox=danger"]
        app = Service(cfg, Path(tmp.name) / "data")
        self.addCleanup(app.store.close)
        app.probe("codex-local")
        work = app.store.get("work", app.send("bot-main", {"text": "x"}, "argv")["work_id"])
        r = app.submit_task("bot-main", work["id"], {"prompt": "go", "outputs": ["result.txt"]})
        deadline = time.monotonic() + 30
        task = app.store.get("task", r["task_id"])
        while time.monotonic() < deadline and task["status"] not in {"succeeded", "failed", "cancelled"}:
            app.task_step()
            time.sleep(.05)
            task = app.store.get("task", r["task_id"])
        self.assertEqual(task["status"], "succeeded", task)
        final_argv = (root / "result.txt").read_text(encoding="utf-8")
        self.assertIn("--strict-config", final_argv)
        self.assertIn("sandbox=danger", final_argv)
        self.assertLess(final_argv.index("--strict-config"), final_argv.index("sandbox=danger"))


if __name__ == "__main__":
    unittest.main()
