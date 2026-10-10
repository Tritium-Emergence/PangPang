import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_bot_runtime import config
from pangpang.execution import Execution, installation_id, validate_profile_env
from pangpang.local_setup import LocalSetup
from pangpang.onboarding import empty_config
from pangpang.service import Service
from pangpang.store import Fault, Store, uid
from pangpang.vault import Vault


class CanonicalIdTests(unittest.TestCase):
    def test_legacy_id_is_byte_identical(self):
        self.assertEqual(installation_id("codex", "local"), "codex-local")
        self.assertEqual(installation_id("codex", "local", None), "codex-local")
        self.assertEqual(installation_id("codex", "local", ""), "codex-local")

    def test_profile_id_and_validation(self):
        self.assertEqual(installation_id("codex", "local", "work"), "codex-work-local")
        for bad in ("Work", "-work", "work_1", "x" * 33, "w ork", 5):
            with self.subTest(profile=bad):
                with self.assertRaises(Fault) as ctx:
                    installation_id("codex", "local", bad)
                self.assertEqual(ctx.exception.code, "invalid_installation_profile")

    def test_env_whitelist(self):
        validate_profile_env({"CODEX_HOME": "/home/a/.codex", "CLAUDE_CONFIG_DIR": "/home/a/.claude"})
        validate_profile_env({})
        for bad in ({"PATH": "/evil"}, {"PANGPANG_TOKEN": "x"}, {"PYTHONPATH": "/evil"},
                    {"CODEX_HOME": ""}, {"CODEX_HOME": 5}, ["not", "a", "dict"],
                    {("a", "b"): "x"}):
            with self.subTest(env=bad):
                with self.assertRaises(Fault) as ctx:
                    validate_profile_env(bad)
                self.assertEqual(ctx.exception.code, "invalid_installation_env")


class InstallationEnvExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def cfg(self, installation_extra):
        cfg = config(self.root / "workspace")
        (self.root / "workspace").mkdir(parents=True, exist_ok=True)
        cfg["installations"][0].update(installation_extra)
        return cfg

    def test_execution_validates_installation_env(self):
        Execution(self.cfg({"env": {"CODEX_HOME": "/home/a/.codex"}}), self.root / "data")
        for bad in ({"PATH": "/evil"}, {"PANGPANG_TOKEN": "x"}, {"CODEX_HOME": 5}):
            with self.subTest(env=bad):
                with self.assertRaises(Fault) as ctx:
                    Execution(self.cfg({"env": bad}), self.root / "data")
                self.assertEqual(ctx.exception.code, "invalid_installation_env")

    def test_spec_merges_env_and_extra_env_wins(self):
        cfg = self.cfg({"env": {"CODEX_HOME": "/profile/home", "CLAUDE_CONFIG_DIR": "/profile/claude"},
                        "extra_env": {"CODEX_HOME": "/materialized/home"}})
        execution = Execution(cfg, self.root / "data")
        task = {"installation_id": "codex-local", "execution_id": "exec-1", "spec_digest": "d",
                "prompt": "x", "inputs": []}
        spec = execution.spec(task)
        self.assertEqual(spec["extra_env"], {"CODEX_HOME": "/materialized/home",
                                             "CLAUDE_CONFIG_DIR": "/profile/claude"})


class DeviceProfileTests(unittest.TestCase):
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

    def test_two_profiles_coexist_with_isolated_env(self):
        self.configure({"adapter": "codex", "profile": "work", "env": {"CODEX_HOME": "/home/a/.codex-work"}})
        self.configure({"adapter": "codex", "profile": "personal", "env": {"CODEX_HOME": "/home/a/.codex-personal"}})
        self.configure({"adapter": "codex"})
        installed = self.holder.execution.installations
        self.assertEqual(sorted(installed), ["codex-local", "codex-personal-local", "codex-work-local"])
        self.assertEqual(installed["codex-work-local"]["env"], {"CODEX_HOME": "/home/a/.codex-work"})
        self.assertEqual(installed["codex-personal-local"]["env"], {"CODEX_HOME": "/home/a/.codex-personal"})
        self.assertNotIn("env", installed["codex-local"])

    def test_reconfigure_keeps_profile_env_until_replaced(self):
        self.configure({"adapter": "codex", "profile": "work", "env": {"CODEX_HOME": "/a"}})
        self.configure({"adapter": "codex", "profile": "work"})
        self.assertEqual(self.holder.execution.installations["codex-work-local"]["env"], {"CODEX_HOME": "/a"})
        self.configure({"adapter": "codex", "profile": "work", "env": {"CODEX_HOME": "/b"}})
        self.assertEqual(self.holder.execution.installations["codex-work-local"]["env"], {"CODEX_HOME": "/b"})
        self.configure({"adapter": "codex", "profile": "work", "env": {}})
        self.assertNotIn("env", self.holder.execution.installations["codex-work-local"])

    def test_legacy_installation_never_gains_a_profile(self):
        self.configure({"adapter": "codex"})
        self.configure({"adapter": "codex"})
        installed = self.holder.execution.installations
        self.assertEqual(list(installed), ["codex-local"])
        self.assertNotIn("profile", installed["codex-local"])

    def test_bad_profile_and_env_rejected_before_write(self):
        for body in ({"adapter": "codex", "profile": "WORK"},
                     {"adapter": "codex", "env": {"PATH": "/evil"}},
                     {"adapter": "codex", "env": {"PANGPANG_TOKEN": "x"}}):
            with self.subTest(body=body):
                with self.assertRaises(Fault):
                    self.configure(body)
        self.assertEqual(self.holder.execution.installations, {})


class OnboardingProfileTests(unittest.TestCase):
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
        work = self.app.send("bot-main", {"text": "connect"}, "profile-work")
        return self.app.onboarding.request("bot-main", work["work_id"], dict(body, host_id=host_id))

    def test_model_cannot_propose_profile_or_env(self):
        host_id = self.paired_host()
        for extra in ({"profile": "work"}, {"env": {"CODEX_HOME": "/x"}}):
            with self.subTest(extra=extra):
                with self.assertRaises(Fault) as ctx:
                    self.request(host_id, dict({"action": "configure", "adapter": "codex"}, **extra))
                self.assertEqual(ctx.exception.code, "operator_owned_setup_field")

    def test_approve_profile_lands_and_mismatched_report_is_refused(self):
        host_id = self.paired_host()
        req = self.request(host_id, {"action": "configure", "adapter": "codex"})
        self.app.onboarding.approve(req["request_id"],
                                    {"profile": "work", "env": {"CODEX_HOME": "/home/a/.codex-work"}})
        command = [c for c in self.app.store.find("command", action="configure")
                   if c["payload"].get("request_id") == req["request_id"]][0]
        self.assertEqual(command["payload"]["installation_id"], "codex-work-" + host_id)
        self.assertEqual(command["payload"]["profile"], "work")
        self.assertEqual(command["payload"]["env"], {"CODEX_HOME": "/home/a/.codex-work"})
        wrong = {"installation": {"id": "codex-" + host_id, "host_id": host_id,
                                  "adapter": "codex", "cwd": str(self.root)}}
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.report(command, wrong)
        self.assertEqual(ctx.exception.code, "installation_approval_mismatch")
        right = {"installation": {"id": "codex-work-" + host_id, "host_id": host_id,
                                  "adapter": "codex", "cwd": str(self.root)}}
        self.app.onboarding.report(command, right)
        landed = self.app.execution.installations["codex-work-" + host_id]
        self.assertEqual(landed["adapter"], "codex")

    def test_approve_rejects_bad_profile_and_env(self):
        host_id = self.paired_host()
        req = self.request(host_id, {"action": "configure", "adapter": "codex"})
        with self.assertRaises(Fault):
            self.app.onboarding.approve(req["request_id"], {"profile": "BAD NAME"})
        req2 = self.request(host_id, {"action": "configure", "adapter": "codex"})
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(req2["request_id"], {"env": {"PANGPANG_TOKEN": "x"}})
        self.assertEqual(ctx.exception.code, "invalid_installation_env")


class AmbiguousIntentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cfg = config(self.root / "workspace")
        (self.root / "workspace").mkdir(parents=True, exist_ok=True)
        base = cfg["installations"][0]
        cfg["installations"] = [
            dict(base, id="codex-local"),
            dict(base, id="codex-work-local", env={"CODEX_HOME": "/a"}),
        ]
        self.app = Service(cfg, self.root / "data")
        self.addCleanup(self.app.store.close)
        self.addCleanup(self.tmp.cleanup)
        self.app.probe("codex-local")
        self.app.probe("codex-work-local")
        self.work = self.app.store.get("work", self.app.send("bot-main", {"text": "x"}, "amb")["work_id"])

    def test_adapter_name_with_two_profiles_rejects_diagnosably(self):
        r = self.app.route("bot-main", self.work, {"installation_id": "codex"})
        self.assertIsNone(r["selected"])
        self.assertEqual(r["intent_ambiguity"]["candidates"], ["codex-local", "codex-work-local"])
        with self.assertRaises(Fault) as ctx:
            self.app.submit_task("bot-main", self.work["id"], {"installation_id": "codex", "prompt": "y"})
        self.assertEqual(ctx.exception.code, "ambiguous_executor")
        self.assertIn("codex-work-local", ctx.exception.advice)

    def test_exact_profile_id_still_routes(self):
        r = self.app.route("bot-main", self.work, {"installation_id": "codex-work-local"})
        self.assertIsNotNone(r["selected"])
        self.assertEqual(r["selected"]["installation_id"], "codex-work-local")


class EnvReachesExecutorTests(unittest.TestCase):
    def test_profile_env_arrives_in_executor_process(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve() / "workspace"
        root.mkdir()
        cfg = config(root)
        cfg["installations"][0]["argv"] = [
            sys.executable, "-c",
            "import os,pathlib; pathlib.Path('result.txt').write_text(os.environ.get('CODEX_HOME',''))"]
        cfg["installations"][0]["env"] = {"CODEX_HOME": "/profile/must-arrive"}
        app = Service(cfg, Path(tmp.name) / "data")
        self.addCleanup(app.store.close)
        app.probe("codex-local")
        work = app.store.get("work", app.send("bot-main", {"text": "x"}, "enve")["work_id"])
        r = app.submit_task("bot-main", work["id"], {"prompt": "go", "outputs": ["result.txt"]})
        deadline = time.monotonic() + 30
        task = app.store.get("task", r["task_id"])
        while time.monotonic() < deadline and task["status"] not in {"succeeded", "failed", "cancelled"}:
            app.task_step()
            time.sleep(.05)
            task = app.store.get("task", r["task_id"])
        self.assertEqual(task["status"], "succeeded", task)
        self.assertEqual((root / "result.txt").read_text(encoding="utf-8"), "/profile/must-arrive")


if __name__ == "__main__":
    unittest.main()
