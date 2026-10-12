"""Executor run authorization.

Installations already stored before this process start are granted always at
the digest captured then. A later installation, or a change to what a run
executes, waits for the existing per-task approval. Unknown adapters stay
unavailable and are not granted.
"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pangpang import consent, runner
from pangpang.connector import Connector, authorization_digest, policy_digest
from pangpang.execution import ADAPTER_CATALOG, Execution
from pangpang.runtime import TOOLS, Runtime
from pangpang.service import Service
from pangpang.store import Fault, Store, launch_contract, contract_digest
from test_bot_runtime import config


UNKNOWN = "not-in-catalog"
CANARIES = (
    "CANARY_ARGV_VALUE",
    "CANARY_ENV_VALUE",
    "CANARY_PROXY_VALUE",
    "CANARY_CREDENTIAL_ID",
    "CANARY_VAULT_VALUE",
    "CANARY_ENDPOINT",
    "CANARY_SETUP_PAYLOAD",
)


def _python_argv(marker):
    script = (
        "import pathlib; pathlib.Path(%r).write_text('ok')" % marker
    )
    return [sys.executable, "-c", script]


def _installation(adapter, argv, workspace, host="host-local", **extra):
    row = {
        "id": "inst-local-" + adapter,
        "host_id": host,
        "adapter": adapter,
        "argv": argv,
        "cwd": str(workspace),
        "enabled": True,
        "prompt_mode": "stdin",
        "risk": "write",
        "timeout_s": 3600,
        "max_concurrency": 1,
        "workspace_key": [host, ["ws"]],
        "workspace_cwd": str(workspace),
        "probe_argv": [sys.executable, "--version"],
    }
    row.update(extra)
    return row


def _managed(workspace, installations):
    from pangpang.onboarding import empty_config
    config = empty_config()
    config["hosts"] = [{
        "id": "host-local", "name": "本机", "transport": "local", "os": "Linux",
        "allowed_roots": [str(workspace)],
        consent.CONSENT_KEY: consent.RISK_VERSION,
    }]
    config["installations"] = installations
    return config


def _settings_blob(db_path):
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT data FROM objects WHERE kind='settings' AND id='managed'").fetchone()
    finally:
        connection.close()
    return row[0]


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.data = self.root / "data"
        self.apps = []
        self.addCleanup(self._close)

    def _close(self):
        for app in self.apps:
            try:
                app.store.close()
            except Exception:
                pass

    def _seed(self, installations, probes=()):
        self.data.mkdir(parents=True, exist_ok=True)
        store = Store(self.data / "bots.db")
        store.set_schema_version(2)
        config = _managed(self.workspace, installations)
        store.put("settings", {"id": "managed", "config": config})
        store.put("task", {
            "id": "task-before", "status": "awaiting_approval",
            "execution_id": "exec-" + "ab" * 16,
            "spec_digest": "digest-before-upgrade", "work_id": "work-before",
            "owner_bot_id": "bot-main", "installation_id": "inst-local-codex",
            "host_id": "host-local", "prompt": "already waiting", "created": 1,
        })
        for probe in probes:
            store.put("probe", probe)
        store.close()
        return _settings_blob(self.data / "bots.db")

    def _start(self, installations):
        before = self._seed(installations, probes=[
            {"id": "inst-local-codex", "available": True, "supervision": "windows-job-object"},
            {"id": "inst-local-" + UNKNOWN, "available": True, "supervision": "windows-job-object"},
        ])
        app = Service({}, self.data, deployment_level="L0")
        self.apps.append(app)
        return app, before

    def _rows(self):
        marker = "ran.txt"
        return [
            _installation("codex", _python_argv(marker), self.workspace),
            _installation(UNKNOWN, ["/usr/bin/" + UNKNOWN, "-p"], self.workspace,
                          max_concurrency=2),
        ], marker

    def test_stored_rows_run_without_a_prompt_and_a_rollback_still_loads(self):
        rows, marker = self._rows()
        app, before = self._start(rows)
        self.assertEqual(app.store.schema_version(), 2)
        self.assertEqual(_settings_blob(self.data / "bots.db"), before)
        host = app.execution.hosts["host-local"]
        self.assertEqual(host.get(consent.CONSENT_KEY), consent.RISK_VERSION)
        legacy = app.store.get("task", "task-before")
        self.assertEqual(legacy["status"], "awaiting_approval")
        self.assertEqual(legacy["spec_digest"], "digest-before-upgrade")
        self.assertNotIn("authorization_digest", legacy)
        approved = app.approve("task-before", {"spec_digest": "digest-before-upgrade"})
        self.assertEqual(approved["status"], "queued")
        self.assertEqual(approved["spec_digest"], "digest-before-upgrade")
        app.cancel("task-before")

        grants = app.store.get("settings", "executor_grants")
        self.assertTrue(grants["migrated"])
        self.assertIn("inst-local-codex", grants["grants"])
        self.assertEqual(grants["grants"]["inst-local-codex"]["origin"], "migration")
        self.assertNotIn("inst-local-" + UNKNOWN, grants["grants"])
        self.assertNotIn("inst-local-" + UNKNOWN, grants.get("pending_device") or [])
        self.assertIn("inst-local-" + UNKNOWN, app.execution.unknown_adapters)
        probed = app.probe("inst-local-" + UNKNOWN)
        self.assertFalse(probed["available"])
        self.assertEqual(probed["reason"], "unknown_adapter")
        self.assertFalse(self.workspace.joinpath(marker).exists())
        route = app.route("bot-main", {"id": "work-1"}, {})
        self.assertEqual(route["selected"]["installation_id"], "inst-local-codex")
        self.assertEqual(_settings_blob(self.data / "bots.db"), before)

        app.probe("inst-local-codex")
        message = app.send("bot-main", {"text": "run", "allow_write": True}, "upgrade")
        submitted = app.submit_task("bot-main", message["work_id"], {
            "prompt": "go", "installation_id": "inst-local-codex"})
        self.assertEqual(submitted["status"], "queued")
        task = app.store.get("task", submitted["task_id"])
        self.assertNotEqual(task.get("status"), "awaiting_approval")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and task["status"] not in {"succeeded", "failed", "cancelled"}:
            app.task_step()
            time.sleep(0.05)
            task = app.store.get("task", task["id"])
        self.assertEqual(task["status"], "succeeded", task.get("error") or task.get("receipt"))
        self.assertEqual((self.workspace / marker).read_text(encoding="utf-8"), "ok")
        self.assertEqual(task["host_id"], "host-local")
        self.assertEqual(task["installation_id"], "inst-local-codex")
        self.assertTrue(task.get("authorization_digest"))
        self.assertIsNone(task.get("device_kb"))
        self.assertIsInstance(task.get("duration"), float)
        receipt = task["receipt"]
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt.get("authorization_digest"), task["authorization_digest"])
        self.assertEqual(receipt.get("host_id"), "host-local")
        self.assertEqual(receipt.get("installation_id"), "inst-local-codex")
        self.assertEqual(receipt.get("duration"), task["duration"])
        self.assertIsNone(receipt.get("device_kb"))
        self.assertEqual(_settings_blob(self.data / "bots.db"), before)

        first_grants = app.store.get("settings", "executor_grants")
        app.store.close()
        self.apps.remove(app)
        again = Service({}, self.data, deployment_level="L0")
        self.apps.append(again)
        self.assertEqual(again.store.get("settings", "executor_grants"), first_grants)
        self.assertEqual(_settings_blob(self.data / "bots.db"), before)

        managed = again.store.get("settings", "managed")
        managed["config"]["installations"].append(_installation(
            "gemini", [sys.executable, "--version"], self.workspace))
        again.store.put("settings", managed)
        again.store.close()
        self.apps.remove(again)
        third = Service({}, self.data, deployment_level="L0")
        self.apps.append(third)
        later = third.store.get("settings", "executor_grants")
        self.assertNotIn("inst-local-gemini", later["grants"])
        self.assertEqual(later["grants"]["inst-local-codex"]["digest"],
                         first_grants["grants"]["inst-local-codex"]["digest"])
        self.assertEqual(later["grants"]["inst-local-codex"]["granted_at"],
                         first_grants["grants"]["inst-local-codex"]["granted_at"])
        third.probe("inst-local-gemini")
        message = third.send("bot-main", {"text": "new", "allow_write": True}, "new-row")
        submitted = third.submit_task("bot-main", message["work_id"], {
            "prompt": "go", "installation_id": "inst-local-gemini"})
        self.assertEqual(submitted["status"], "awaiting_approval")
        codex = next(i for i in third.execution.installations.values() if i["id"] == "inst-local-codex")
        self.assertEqual(codex["argv"], rows[0]["argv"])

        third.store.close()
        self.apps.remove(third)
        store = Store(self.data / "bots.db")
        self.assertEqual(store.schema_version(), 2)
        loaded = store.get("settings", "managed")["config"]
        execution = Execution(loaded, self.data, tolerate_unknown_adapters=True)
        self.assertEqual(execution.installations["inst-local-codex"]["argv"], rows[0]["argv"])
        self.assertIn("inst-local-" + UNKNOWN, execution.unknown_adapters)
        self.assertIn("executor_grants", {row["id"] for row in store.find("settings")})
        store.close()

    def test_grandfathered_codex_keeps_stored_argv_and_runs_without_a_prompt(self):
        # Historical catalog command: exec with no sandbox flag. The upgrade
        # grant is the digest of this stored argv, not the new catalog argv.
        stored = [sys.executable, "exec", "-"]
        app, before = self._start([_installation("codex", stored, self.workspace)])
        live = app.execution.installations["inst-local-codex"]
        self.assertEqual(live["argv"], stored)
        self.assertEqual(_settings_blob(self.data / "bots.db"), before)
        self.assertNotEqual(ADAPTER_CATALOG["codex"]["argv"], ["exec", "-"])
        self.assertIn("workspace-write", ADAPTER_CATALOG["codex"]["argv"])
        grant = app.store.get("settings", "executor_grants")["grants"]["inst-local-codex"]
        self.assertEqual(grant["origin"], "migration")
        self.assertEqual(grant["digest"], app._authorization_digest("inst-local-codex"))
        planned = runner.codex_app_server_plan(live["argv"])
        self.assertNotIn("sandbox", planned[1])
        self.assertNotEqual(planned[1].get("sandbox"), "danger-full-access")
        self.assertNotEqual(planned[1].get("sandbox"), "workspace-write")
        app.probe("inst-local-codex")
        message = app.send("bot-main", {"text": "run", "allow_write": True}, "grandfather")
        submitted = app.submit_task("bot-main", message["work_id"], {
            "prompt": "go", "installation_id": "inst-local-codex"})
        self.assertEqual(submitted["status"], "queued")
        task = app.store.get("task", submitted["task_id"])
        self.assertNotEqual(task["status"], "awaiting_approval")
        self.assertEqual(app.execution.installations["inst-local-codex"]["argv"], stored)
        app.cancel(submitted["task_id"])


class RunGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "workspace"
        self.root.mkdir()
        self.cfg = config(self.root)
        self.cfg["installations"][0]["argv"] = _python_argv("ran.txt")
        self.cfg["installations"][0]["probe_argv"] = [sys.executable, "--version"]
        self.cfg["installations"][0]["enabled"] = True
        self.app = Service(self.cfg, self.root.parent / "data", deployment_level="L0")
        self.addCleanup(self.app.store.close)
        self.app.probe("codex-local")

    def _work(self, key):
        return self.app.send("bot-main", {"text": key, "allow_write": True}, key)["work_id"]

    def _drop_grant(self):
        row = self.app.store.get("settings", "executor_grants")
        row["grants"].pop("codex-local", None)
        self.app.store.put("settings", row)

    def _run(self, task_id):
        deadline = time.monotonic() + 30
        task = self.app.store.get("task", task_id)
        while time.monotonic() < deadline and task["status"] not in {"succeeded", "failed", "cancelled"}:
            self.app.task_step()
            time.sleep(0.05)
            task = self.app.store.get("task", task_id)
        return task

    def test_unauthorized_never_starts_and_once_is_bound_to_the_task(self):
        self._drop_grant()
        submitted = self.app.submit_task("bot-main", self._work("once"), {
            "prompt": "go", "installation_id": "codex-local"})
        self.assertEqual(submitted["status"], "awaiting_approval")
        self.app.task_step()
        task = self.app.store.get("task", submitted["task_id"])
        self.assertEqual(task["status"], "awaiting_approval")
        self.assertFalse((self.root / "ran.txt").exists())
        self.assertIs(self.app._start_denied(task), "installation_not_authorized")
        self.app.approve(task["id"], {"spec_digest": task["spec_digest"]})
        task = self.app.store.get("task", task["id"])
        self.assertEqual(task["status"], "queued")
        self.assertIsNone(self.app._start_denied(task))
        finished = self._run(task["id"])
        self.assertEqual(finished["status"], "succeeded", finished.get("error"))
        self.assertTrue((self.root / "ran.txt").exists())
        (self.root / "ran.txt").unlink()
        again = self.app.submit_task("bot-main", self._work("next"), {
            "prompt": "go", "installation_id": "codex-local"})
        self.assertEqual(again["status"], "awaiting_approval")
        self.app.task_step()
        self.assertFalse((self.root / "ran.txt").exists())

    def test_config_change_between_approve_and_start_starts_nothing(self):
        self._drop_grant()
        submitted = self.app.submit_task("bot-main", self._work("toctou"), {
            "prompt": "go", "installation_id": "codex-local"})
        task = self.app.store.get("task", submitted["task_id"])
        self.app.approve(task["id"], {"spec_digest": task["spec_digest"]})
        self.app.configure_installation("codex-local", {"extra_argv": ["--strict-config"]})
        finished = self._run(task["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error"], "installation_not_authorized")
        self.assertFalse((self.root / "ran.txt").exists())

    def test_always_survives_routing_and_breaks_when_execution_changes(self):
        message = self.app.submit_task("bot-main", self._work("always"), {
            "prompt": "go", "installation_id": "codex-local"})
        self.assertEqual(message["status"], "queued")
        self.app.cancel(message["task_id"])
        self.app.configure_installation("codex-local", {"priority": 7, "use_when": "notes only"})
        routed = self.app.submit_task("bot-main", self._work("routed"), {
            "prompt": "go", "installation_id": "codex-local"})
        self.assertEqual(routed["status"], "queued")
        self.app.cancel(routed["task_id"])
        (self.root / "elsewhere").mkdir()

        def rewrite(**fields):
            candidate, revision = self.app.managed_snapshot()
            for item in candidate["installations"]:
                if item["id"] == "codex-local":
                    item.update(fields)
            self.app.commit_managed_config(candidate, base_revision=revision)

        changes = (
            ("extra_argv", lambda: self.app.configure_installation(
                "codex-local", {"extra_argv": ["--strict-config"]})),
            ("env", lambda: rewrite(env={"CODEX_HOME": str(self.root / "home")})),
            ("secret_refs", lambda: rewrite(secret_refs={"CODEX_API_KEY": "cli-ref"})),
            ("cwd", lambda: self.app.configure_installation(
                "codex-local", {"cwd": str(self.root / "elsewhere")})),
        )
        for name, apply in changes:
            apply()
            submitted = self.app.submit_task("bot-main", self._work(name), {
                "prompt": "go", "installation_id": "codex-local"})
            self.assertEqual(submitted["status"], "awaiting_approval", name)
            self.app.cancel(submitted["task_id"])
        host = dict(self.app.execution.hosts["local"])
        candidate = json.loads(json.dumps(self.app.config))
        for item in candidate["hosts"]:
            if item["id"] == "local":
                item[consent.CONSENT_KEY] = "other-version"
        self.app.commit_managed_config(candidate)
        submitted = self.app.submit_task("bot-main", self._work("consent"), {
            "prompt": "go", "installation_id": "codex-local"})
        self.assertEqual(submitted["status"], "awaiting_approval")
        self.assertEqual(host.get(consent.CONSENT_KEY), consent.RISK_VERSION)

    def test_always_on_approve_grants_until_execution_changes(self):
        self._drop_grant()
        submitted = self.app.submit_task("bot-main", self._work("always-once"), {
            "prompt": "go", "installation_id": "codex-local"})
        task = self.app.store.get("task", submitted["task_id"])
        self.assertEqual(list(task["run_preview"]["command"]), list(self.app.execution.installations["codex-local"]["argv"]))
        self.assertEqual(task["run_preview"]["allowed_roots"], [str(self.root)])
        self.assertNotIn("truncated", task["run_preview"])
        self.app.approve(task["id"], {"spec_digest": task["spec_digest"], "always": True})
        grant = self.app.store.get("settings", "executor_grants")["grants"]["codex-local"]
        self.assertEqual(grant["origin"], "operator")
        self.assertEqual(grant["digest"], self.app._authorization_digest("codex-local"))
        nxt = self.app.submit_task("bot-main", self._work("always-next"), {
            "prompt": "go", "installation_id": "codex-local"})
        self.assertEqual(nxt["status"], "queued")
        self.assertNotIn("installation_authorization", self.app.store.get("task", nxt["task_id"]))
        self.app.cancel(nxt["task_id"])
        self.app.configure_installation("codex-local", {"extra_argv": ["--strict-config"]})
        waited = self.app.submit_task("bot-main", self._work("always-argv"), {
            "prompt": "go", "installation_id": "codex-local"})
        self.assertEqual(waited["status"], "awaiting_approval")

    def test_once_approval_does_not_write_a_grant(self):
        self._drop_grant()
        for extra in ({}, {"always": False}):
            submitted = self.app.submit_task("bot-main", self._work("once-" + str(bool(extra))), {
                "prompt": "go", "installation_id": "codex-local"})
            task = self.app.store.get("task", submitted["task_id"])
            self.app.approve(task["id"], dict({"spec_digest": task["spec_digest"]}, **extra))
            self.assertNotIn("codex-local", self.app.store.get("settings", "executor_grants")["grants"])
            self.app.cancel(task["id"])
        submitted = self.app.submit_task("bot-main", self._work("bad-always"), {
            "prompt": "go", "installation_id": "codex-local"})
        task = self.app.store.get("task", submitted["task_id"])
        with self.assertRaises(Fault) as ctx:
            self.app.approve(task["id"], {"spec_digest": task["spec_digest"], "always": "yes"})
        self.assertEqual(ctx.exception.code, "invalid_task_approval")
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "awaiting_approval")

    def test_receipt_comparison_facts_come_from_the_task(self):
        submitted = self.app.submit_task("bot-main", self._work("receipt"), {
            "prompt": "go", "installation_id": "codex-local"})
        task = self.app.store.get("task", submitted["task_id"])
        forged = {
            "execution_id": task["execution_id"], "spec_digest": task["spec_digest"],
            "status": "cancelled", "process_tree_confirmed": True, "not_launched": "queued_cancel",
            "artifacts": [], "host_id": "attacker-host", "installation_id": "attacker-inst",
            "authorization_digest": "forged-auth", "policy_digest": "forged-policy",
            "device_kb": "forged-kb",
        }
        done = self.app.complete_task(task, forged)
        stored = done["receipt"]
        self.assertEqual(stored["host_id"], task["host_id"])
        self.assertEqual(stored["installation_id"], task["installation_id"])
        self.assertEqual(stored["authorization_digest"], task["authorization_digest"])
        self.assertNotIn("policy_digest", stored)
        self.assertIsNone(stored.get("device_kb"))
        self.assertEqual(forged["host_id"], "attacker-host")
        self.assertEqual(forged["policy_digest"], "forged-policy")
        self.assertEqual(forged["device_kb"], "forged-kb")

    def test_cancel_does_not_start_a_process(self):
        self._drop_grant()
        submitted = self.app.submit_task("bot-main", self._work("deny"), {
            "prompt": "go", "installation_id": "codex-local"})
        self.app.cancel(submitted["task_id"])
        self.app.task_step()
        task = self.app.store.get("task", submitted["task_id"])
        self.assertEqual(task["status"], "cancelled")
        self.assertFalse((self.root / "ran.txt").exists())

    def test_priority_is_not_part_of_the_authorization_digest(self):
        before = authorization_digest(self.app.config, "codex-local")
        self.app.configure_installation("codex-local", {"priority": 4, "use_when": "notes"})
        self.assertEqual(authorization_digest(self.app.config, "codex-local"), before)
        self.app.configure_installation("codex-local", {"extra_argv": ["--strict-config"]})
        self.assertNotEqual(authorization_digest(self.app.config, "codex-local"), before)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "workspace"
        self.root.mkdir()
        cfg = config(self.root)
        cfg["installations"][0].update(
            argv=[sys.executable, "CANARY_ARGV_VALUE"],
            env={"CODEX_HOME": "CANARY_ENV_VALUE"},
            extra_env={"HTTP_PROXY": "CANARY_PROXY_VALUE"},
            secret_refs={"CODEX_API_KEY": "CANARY_CREDENTIAL_ID"},
            endpoint="https://CANARY_ENDPOINT.example",
            profile="notes",
            enabled=True,
            probe_argv=[sys.executable, "--version"],
        )
        self.app = Service(cfg, self.root.parent / "data", deployment_level="L0")
        self.addCleanup(self.app.store.close)
        self.app.vault.set("CANARY_CREDENTIAL_ID", "CANARY_VAULT_VALUE")
        self.app.store.put("setup_request", {
            "id": "setup-canary", "host_id": "local", "status": "needs_approval",
            "proposal": {"payload": "CANARY_SETUP_PAYLOAD", "endpoint": "https://CANARY_ENDPOINT.example"},
            "expires": time.time() + 10,
        })
        self.app.probe("codex-local")
        self.work = self.app.send("bot-main", {"text": "tools", "allow_write": True}, "tools")["work_id"]

    def test_list_executors_is_an_allowlist(self):
        listed = self.app.list_executors()
        text = json.dumps(listed)
        for canary in CANARIES:
            self.assertNotIn(canary, text)
        row = listed["installations"][0]
        self.assertEqual(row["installation_id"], "codex-local")
        self.assertEqual(row["adapter"], "codex")
        self.assertEqual(row["profile"], "notes")
        self.assertEqual(row["host_id"], "local")
        self.assertEqual(row["authorization"], "always")
        self.assertIn("priority", {item["name"] for item in listed["parameters"]})
        adapters = {row["adapter"]: row["parameters"] for row in listed["adapters"]}
        self.assertIn("codex", adapters)
        self.assertIn("priority", adapters["codex"])
        self.assertNotIn("--permission-mode", text)
        self.assertFalse(listed["truncated"])
        names = [tool["function"]["name"] for tool in TOOLS]
        self.assertNotIn("approve", names)
        child = self.app.create_bot({"name": "helper", "role": "notes"})
        runtime = Runtime(self.app, type("M", (), {"complete": lambda *a, **k: ({"content": "x"}, {})})())
        hidden = {tool["function"]["name"] for tool in runtime.tools_for({"bot_id": child["id"]})}
        self.assertNotIn("list_executors", hidden)
        self.assertNotIn("propose_executor", hidden)
        with self.assertRaises(Fault):
            runtime.invoke({"bot_id": "bot-main", "work_id": self.work}, "approve", {})

    def test_propose_applies_routing_and_stages_execution_changes(self):
        argv = list(self.app.execution.installations["codex-local"]["argv"])
        applied = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-local", "priority": 8, "use_when": "notes"})
        self.assertEqual(applied["applied"], ["priority", "use_when"])
        self.assertEqual(self.app.execution.installations["codex-local"]["argv"], argv)
        self.assertEqual(self.app.execution.installations["codex-local"]["priority"], 8)
        staged = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-local", "extra_argv": ["--strict-config"],
            "env": {"CODEX_HOME": str(self.root / "profile")}})
        self.assertEqual(sorted(staged["staged"]["fields"]), ["env", "extra_argv"])
        self.assertNotIn("extra_argv", self.app.execution.installations["codex-local"])
        request_id = staged["staged"]["request_id"]
        before_grant = dict(self.app.store.get("settings", "executor_grants")["grants"]["codex-local"])
        self.app.onboarding.approve(request_id, {})
        self.assertEqual(self.app.execution.installations["codex-local"]["extra_argv"], ["--strict-config"])
        grant = self.app.store.get("settings", "executor_grants")["grants"]["codex-local"]
        self.assertEqual(grant, before_grant)
        self.assertEqual(grant["origin"], "migration")
        self.assertNotEqual(grant["digest"], authorization_digest(self.app.config, "codex-local"))
        with self.assertRaises(Fault):
            self.app.onboarding.approve(request_id, {})
        lowered = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-local", "max_concurrency": 1, "timeout_s": 60, "enabled": False})
        # max_concurrency is already 1, so only the reductions that change are applied.
        self.assertIn("applied", lowered)
        self.assertFalse(self.app.execution.installations["codex-local"]["enabled"])
        self.assertEqual(self.app.execution.installations["codex-local"]["timeout_s"], 60)
        self.app.configure_installation("codex-local", {"enabled": True, "max_concurrency": 1, "timeout_s": 3600})
        widened = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-local", "max_concurrency": 3})
        self.assertEqual(widened["staged"]["fields"], ["max_concurrency"])
        self.assertEqual(self.app.execution.installations["codex-local"].get("max_concurrency", 1), 1)
        (self.root / "cas").mkdir()
        denied = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-local", "cwd": str(self.root / "other")})
        self.app.onboarding.deny(denied["staged"]["request_id"])
        self.assertEqual(self.app.execution.installations["codex-local"]["cwd"], str(self.root))
        expired = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-local", "cwd": str(self.root / "later")})
        req = self.app.store.get("setup_request", expired["staged"]["request_id"])
        req["expires"] = time.time() - 5
        self.app.store.put("setup_request", req)
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(req["id"], {})
        self.assertEqual(ctx.exception.code, "setup_request_expired_or_used")
        self.assertEqual(self.app.execution.installations["codex-local"]["cwd"], str(self.root))
        conflicting = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-local", "cwd": str(self.root / "cas")})
        self.app.configure_installation("codex-local", {"priority": 9})
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(conflicting["staged"]["request_id"], {})
        self.assertEqual(ctx.exception.code, "installation_config_changed")
        self.assertNotIn("cas", self.app.execution.installations["codex-local"]["cwd"])
        with self.assertRaises(Fault):
            self.app.propose_executor("bot-main", self.work, {
                "installation_id": "missing", "priority": 1})

    def test_local_profile_waits_for_setup_approval_then_asks_on_the_first_run(self):
        before = set(self.app.execution.installations)
        with self.assertRaises(Fault) as ctx:
            self.app.propose_executor("bot-main", self.work, {
                "host_id": "local", "adapter": "codex", "profile": "work",
                "endpoint": "https://127.0.0.1:9/v1"})
        self.assertEqual(ctx.exception.code, "operator_owned_setup_field")
        self.assertEqual(set(self.app.execution.installations), before)
        staged = self.app.propose_executor("bot-main", self.work, {
            "host_id": "local", "adapter": "codex", "profile": "work",
            "cwd": str(self.root), "env": {"CODEX_HOME": str(self.root / "profile")}})
        self.assertEqual(set(self.app.execution.installations), before)
        request = self.app.store.get("setup_request", staged["staged"]["request_id"])
        self.assertEqual(request["status"], "needs_approval")
        self.assertEqual(request["proposal"]["action"], "executor_config")
        self.assertIsNone(request["proposal"]["installation_id"])
        denied = self.app.propose_executor("bot-main", self.work, {
            "host_id": "local", "adapter": "codex", "profile": "dropped"})
        self.app.onboarding.deny(denied["staged"]["request_id"])
        self.assertNotIn("codex-dropped-local", self.app.execution.installations)
        expired = self.app.propose_executor("bot-main", self.work, {
            "host_id": "local", "adapter": "codex", "profile": "stale"})
        stale = self.app.store.get("setup_request", expired["staged"]["request_id"])
        stale["expires"] = time.time() - 5
        self.app.store.put("setup_request", stale)
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(stale["id"], {})
        self.assertEqual(ctx.exception.code, "setup_request_expired_or_used")
        self.assertNotIn("codex-stale-local", self.app.execution.installations)
        sibling = self.app.execution.installations["codex-local"]
        expected_argv = [sibling["argv"][0], *ADAPTER_CATALOG["codex"]["argv"]]
        self.assertEqual(request["proposal"]["command"], expected_argv)
        request["proposal"]["command"] = ["evil-binary", "--not-catalog"]
        self.app.store.put("setup_request", request)
        endpoint = "https://127.0.0.1:9/v1"
        approved = self.app.onboarding.approve(request["id"], {"endpoint": endpoint})
        iid = "codex-work-local"
        self.assertEqual(approved["installation_id"], iid)
        created = self.app.execution.installations[iid]
        self.assertEqual(created["argv"], expected_argv)
        self.assertNotEqual(created["argv"], sibling["argv"])
        self.assertEqual(created["profile"], "work")
        self.assertEqual(created["extra_env"]["OPENAI_BASE_URL"], endpoint)
        self.assertEqual(created["env"]["CODEX_HOME"], str(self.root / "profile"))
        self.assertNotIn(iid, self.app.store.get("settings", "executor_grants")["grants"])
        self.app.probe(iid)
        submitted = self.app.submit_task("bot-main", self.work, {
            "prompt": "go", "installation_id": iid})
        self.assertEqual(submitted["status"], "awaiting_approval")
        self.app.cancel(submitted["task_id"])
        again = self.app.propose_executor("bot-main", self.work, {
            "host_id": "local", "adapter": "codex", "profile": "work"})
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(again["staged"]["request_id"], {})
        self.assertEqual(ctx.exception.code, "installation_already_exists")
        self.assertEqual(self.app.execution.installations[iid]["extra_env"]["OPENAI_BASE_URL"], endpoint)
        missing = self.app.propose_executor("bot-main", self.work, {
            "host_id": "local", "adapter": "gemini", "profile": "work"})
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(missing["staged"]["request_id"], {})
        self.assertEqual(ctx.exception.code, "cli_not_installed")
        self.assertNotIn("gemini-work-local", self.app.execution.installations)
        with self.assertRaises(Fault) as ctx:
            self.app.propose_executor("bot-main", self.work, {
                "host_id": "missing", "adapter": "codex"})
        self.assertEqual(ctx.exception.code, "connector_host_required")

    def test_blank_endpoint_uses_catalog_env_and_keeps_routing(self):
        staged = self.app.propose_executor("bot-main", self.work, {
            "host_id": "local", "adapter": "codex", "profile": "plain",
            "cwd": str(self.root), "priority": 4, "use_when": "notes only"})
        self.assertEqual(sorted(staged["staged"]["fields"]),
                         ["adapter", "cwd", "priority", "profile", "use_when"])
        request = self.app.store.get("setup_request", staged["staged"]["request_id"])
        sibling = self.app.execution.installations["codex-local"]
        expected = [sibling["argv"][0], *ADAPTER_CATALOG["codex"]["argv"]]
        self.assertEqual(request["proposal"]["command"], expected)
        self.assertEqual(request["proposal"]["draft"]["priority"], 4)
        self.assertEqual(request["proposal"]["draft"]["use_when"], "notes only")
        approved = self.app.onboarding.approve(request["id"], {"endpoint": ""})
        row = self.app.execution.installations[approved["installation_id"]]
        self.assertEqual(row["argv"], expected)
        self.assertNotIn("extra_env", row)
        self.assertNotIn("CANARY_PROXY_VALUE", json.dumps(row))
        self.assertEqual(row["priority"], 4)
        self.assertEqual(row["use_when"], "notes only")
        self.assertNotIn(row["id"], self.app.store.get("settings", "executor_grants")["grants"])
        self.app.probe(row["id"])
        submitted = self.app.submit_task("bot-main", self.work, {
            "prompt": "go", "installation_id": row["id"]})
        self.assertEqual(submitted["status"], "awaiting_approval")
        self.app.cancel(submitted["task_id"])
        bad = self.app.propose_executor("bot-main", self.work, {
            "host_id": "local", "adapter": "codex", "profile": "bad", "priority": 0})
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(bad["staged"]["request_id"], {})
        self.assertEqual(ctx.exception.code, "invalid_priority")
        self.assertNotIn("codex-bad-local", self.app.execution.installations)

    def test_new_local_profile_rejects_enabled_and_limits(self):
        sibling = self.app.execution.installations["codex-local"]
        before = {k: sibling.get(k) for k in ("enabled", "max_concurrency", "timeout_s")}
        before_ids = [row["id"] for row in self.app.store.find("setup_request")]
        installs = set(self.app.execution.installations)
        base = {"host_id": "local", "adapter": "codex", "profile": "limited"}
        for extra in ({"enabled": False}, {"max_concurrency": 2}, {"timeout_s": 30},
                      {"enabled": False, "max_concurrency": 2, "timeout_s": 30}):
            with self.assertRaises(Fault) as ctx:
                self.app.propose_executor("bot-main", self.work, dict(base, **extra))
            self.assertEqual(ctx.exception.code, "unsupported_installation_field")
        self.assertEqual(set(self.app.execution.installations), installs)
        self.assertEqual([row["id"] for row in self.app.store.find("setup_request")], before_ids)
        self.assertEqual({k: self.app.execution.installations["codex-local"].get(k)
                          for k in before}, before)

    def test_invalid_staged_env_does_not_apply_priority(self):
        before = self.app.execution.installations["codex-local"].get("priority")
        before_ids = [row["id"] for row in self.app.store.find("setup_request")]
        with self.assertRaises(Fault) as ctx:
            self.app.propose_executor("bot-main", self.work, {
                "installation_id": "codex-local", "priority": 11,
                "env": {"NOT_A_PROFILE_KEY": "/tmp"}})
        self.assertEqual(ctx.exception.code, "invalid_installation_env")
        self.assertEqual(self.app.execution.installations["codex-local"].get("priority"), before)
        self.assertEqual([row["id"] for row in self.app.store.find("setup_request")], before_ids)
        with self.assertRaises(Fault) as ctx:
            self.app.propose_executor("bot-main", self.work, {
                "installation_id": "codex-local", "priority": 12, "profile": "notes"})
        self.assertEqual(ctx.exception.code, "installation_approval_mismatch")
        self.assertEqual(self.app.execution.installations["codex-local"].get("priority"), before)

    def test_reprobe_uses_the_stored_installation(self):
        runtime = Runtime(self.app, type("M", (), {"complete": lambda *a, **k: ({"content": "x"}, {})})())
        result = runtime.invoke(
            {"bot_id": "bot-main", "work_id": self.work}, "setup_agent",
            {"host_id": "local", "action": "probe", "installation_id": "codex-local"})
        self.assertTrue(result["probed"])
        self.assertTrue(result["available"])
        self.assertFalse((self.root / "ran.txt").exists())


class ConnectorDigestTests(unittest.TestCase):
    def test_device_refuses_a_digest_mismatch_without_starting(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve() / "workspace"
        root.mkdir()
        local = config(root)
        local["installations"][0]["enabled"] = True
        local.update(host_id="local", cloud_url="http://127.0.0.1:1", credential_env="TEST_DEVICE_KEY")
        connector = Connector(local, root.parent / "device")
        self.addCleanup(connector.close)
        started = {"called": False}

        def boom(_task):
            started["called"] = True
            raise AssertionError("process started")

        connector.execution.start = boom
        execution_id = "exec-" + ("ab" * 16)
        task = {
            "id": "task-1", "execution_id": execution_id, "host_id": "local",
            "installation_id": "codex-local", "inputs": [], "outputs": [], "prompt": "go",
            "spec_digest": "spec", "cwd": str(root), "timeout_s": 30, "access_mode": "write",
            "supervision_floor": "windows-job-object", "attempt_uuid": "a" * 32,
            "policy_digest": policy_digest(connector.config, "codex-local"),
            "authorization_digest": "not-the-live-digest",
        }
        task["contract"] = launch_contract(task)
        task["contract_digest"] = contract_digest(task)
        task["min_contract_version"] = task["contract"]["contract_version"]
        result = connector.start(task)
        self.assertTrue(result.get("rejected"))
        self.assertFalse(started["called"])
        receipt = connector.store.get("local_run", execution_id)["receipt"]
        self.assertEqual(receipt["error"], "local_policy_changed_probe_and_resubmit")
        self.assertEqual(receipt.get("not_launched"), "device_rejected")


class PiBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "workspace"
        self.root.mkdir()
        self.pi_dir = Path(self.tmp.name).resolve() / "pi-home"
        cfg = config(self.root)
        cfg["models"][0]["credential_ref"] = "key-a"
        cfg["installations"] = [{
            "id": "inst-local-pi", "host_id": "local", "adapter": "pi",
            "argv": [sys.executable, "--version"], "cwd": str(self.root),
            "enabled": True, "prompt_mode": "stdin", "risk": "write",
            "probe_argv": [sys.executable, "--version"],
            "extra_env": {"PI_CODING_AGENT_DIR": str(self.pi_dir)},
            "max_concurrency": 1,
        }]
        self.app = Service(cfg, self.root.parent / "data", deployment_level="L0")
        self.addCleanup(self.app.store.close)
        self.app.probe("inst-local-pi")

    def _work(self, key):
        return self.app.send("bot-main", {"text": key, "allow_write": True}, key)["work_id"]

    def _set_profile(self, **fields):
        candidate, revision = self.app.managed_snapshot()
        candidate["models"][0].update(fields)
        self.app.commit_managed_config(candidate, base_revision=revision)

    def test_credential_change_reasks_and_launch_overlay_does_not(self):
        first = self.app.submit_task("bot-main", self._work("pi-a"), {
            "prompt": "go", "installation_id": "inst-local-pi"})
        self.assertEqual(first["status"], "queued")
        self.app.cancel(first["task_id"])
        inst = self.app.execution.installations["inst-local-pi"]
        self.assertTrue(self.app._bind_profile_pi(inst))
        self.assertEqual(inst["secret_refs"]["OPENAI_API_KEY"], "key-a")
        bound = self.app.submit_task("bot-main", self._work("pi-bound"), {
            "prompt": "go", "installation_id": "inst-local-pi"})
        self.assertEqual(bound["status"], "queued")
        frozen = self.app.store.get("task", bound["task_id"])
        self._set_profile(credential_ref="key-b")
        self.assertEqual(self.app._start_denied(frozen), "installation_not_authorized")
        waited = self.app.submit_task("bot-main", self._work("pi-b"), {
            "prompt": "go", "installation_id": "inst-local-pi"})
        self.assertEqual(waited["status"], "awaiting_approval")
        task = self.app.store.get("task", waited["task_id"])
        self.app.approve(task["id"], {"spec_digest": task["spec_digest"], "always": True})
        granted = self.app.submit_task("bot-main", self._work("pi-granted"), {
            "prompt": "go", "installation_id": "inst-local-pi"})
        self.assertEqual(granted["status"], "queued")
        self.app.cancel(granted["task_id"])
        self._set_profile(credential_env="OTHER_ENV")
        changed = self.app.submit_task("bot-main", self._work("pi-env"), {
            "prompt": "go", "installation_id": "inst-local-pi"})
        self.assertEqual(changed["status"], "awaiting_approval")


class ConnectorProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "workspace"
        self.root.mkdir()
        cfg = config(self.root)
        cfg["hosts"].append({
            "id": "pc", "name": "PC", "transport": "connector", "os": "Linux",
            "allowed_roots": ["/work"], "credential_env": "TEST_DEVICE_KEY",
            "credential_hash": "ab" * 32,
        })
        cfg["installations"].append({
            "id": "codex-work-pc", "host_id": "pc", "adapter": "codex", "profile": "work",
            "argv": ["codex", "exec", "-"], "cwd": "/work", "enabled": True,
            "priority": 30, "prompt_mode": "stdin", "risk": "write",
        })
        self.app = Service(cfg, self.root.parent / "data", deployment_level="L0")
        self.addCleanup(self.app.store.close)
        self.app.store.put("device", {"id": "pc", "revoked": False, "public_key": "k"})
        self.work = self.app.send("bot-main", {"text": "remote", "allow_write": True}, "remote")["work_id"]

    def test_remote_unsupported_fields_change_nothing(self):
        before = self.app.execution.installations["codex-work-pc"]["priority"]
        before_ids = [row["id"] for row in self.app.store.find("setup_request")]
        with self.assertRaises(Fault) as ctx:
            self.app.propose_executor("bot-main", self.work, {
                "installation_id": "codex-work-pc", "priority": 4,
                "secret_refs": {"CODEX_API_KEY": "cli-ref"}, "enabled": False,
                "timeout_s": 30, "max_concurrency": 4})
        self.assertEqual(ctx.exception.code, "unsupported_installation_field")
        self.assertEqual(self.app.execution.installations["codex-work-pc"]["priority"], before)
        self.assertEqual([row["id"] for row in self.app.store.find("setup_request")], before_ids)
        with self.assertRaises(Fault) as ctx:
            self.app.propose_executor("bot-main", self.work, {
                "installation_id": "codex-work-pc", "priority": 2, "profile": "other"})
        self.assertEqual(ctx.exception.code, "installation_approval_mismatch")
        self.assertEqual(self.app.execution.installations["codex-work-pc"]["priority"], before)

    def test_remote_approval_keeps_the_profiled_id_and_cwd(self):
        staged = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-work-pc", "cwd": "/work/src",
            "extra_argv": ["--strict-config"]})
        self.app.onboarding.approve(staged["staged"]["request_id"], {})
        command = self.app.store.find("command", action="configure")[0]
        payload = command["payload"]
        self.assertEqual(payload["installation_id"], "codex-work-pc")
        self.assertEqual(payload["profile"], "work")
        self.assertEqual(payload["cwd"], "/work/src")
        self.assertEqual(payload["extra_argv"], ["--strict-config"])
        self.assertNotIn("secret_refs", payload.get("draft") or {})
        self.app.onboarding.report(command, {"installation": {
            "id": "codex-work-pc", "host_id": "pc", "adapter": "codex", "cwd": "/work/src",
            "resource_keys": [], "max_concurrency": 1}})
        landed = self.app.execution.installations["codex-work-pc"]
        self.assertEqual(landed.get("profile"), "work")
        again = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-work-pc", "extra_argv": ["--strict-config"]})
        self.app.onboarding.approve(again["staged"]["request_id"], {})
        follow = [c for c in self.app.store.find("command", action="configure")
                  if c["payload"].get("request_id") == again["staged"]["request_id"]][0]
        self.assertEqual(follow["payload"]["installation_id"], "codex-work-pc")
        self.assertEqual(follow["payload"]["profile"], "work")

    def test_setup_report_does_not_write_a_run_grant(self):
        pending = self.app.store.get("settings", "executor_grants")
        self.assertIn("codex-work-pc", pending["pending_device"])
        self.assertNotIn("codex-work-pc", pending["grants"])
        staged = self.app.propose_executor("bot-main", self.work, {
            "host_id": "pc", "adapter": "codex", "profile": "notes", "cwd": "/work"})
        self.app.onboarding.approve(staged["staged"]["request_id"], {"endpoint": ""})
        command = self.app.store.find("command", action="configure")[0]
        self.app.onboarding.report(command, {
            "installation": {"id": "codex-notes-pc", "host_id": "pc", "adapter": "codex",
                             "cwd": "/work", "resource_keys": [], "max_concurrency": 1},
            "probe": {"available": True, "supervision": "windows-job-object",
                      "authorization_digest": "device-digest"},
        })
        grants = self.app.store.get("settings", "executor_grants")
        self.assertNotIn("codex-notes-pc", grants["grants"])
        self.assertIn("codex-work-pc", grants["pending_device"])
        migrated = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-work-pc", "cwd": "/work/src"})
        self.app.onboarding.approve(migrated["staged"]["request_id"], {})
        follow = [c for c in self.app.store.find("command", action="configure")
                  if c["payload"].get("request_id") == migrated["staged"]["request_id"]][0]
        self.app.onboarding.report(follow, {
            "installation": {"id": "codex-work-pc", "host_id": "pc", "adapter": "codex",
                             "cwd": "/work/src", "resource_keys": [], "max_concurrency": 1},
            "probe": {"available": True, "supervision": "windows-job-object",
                      "policy_digest": "migrated-digest"},
        })
        stored = self.app.store.get("settings", "executor_grants")
        self.assertNotIn("codex-work-pc", stored["grants"])
        self.assertNotIn("codex-work-pc", stored["pending_device"])
        self.assertNotIn("codex-notes-pc", stored["grants"])
        self.assertEqual(self.app.store.get("setup_request", migrated["staged"]["request_id"])["status"], "done")
        self.assertFalse(self.app._grant_matches(self.app.execution.installations["codex-work-pc"]))
        self.app.broker._last_seen["pc"] = time.time()
        submitted = self.app.submit_task("bot-main", self.work, {
            "prompt": "go", "installation_id": "codex-work-pc"})
        self.assertEqual(submitted["status"], "awaiting_approval")
        task = self.app.store.get("task", submitted["task_id"])
        self.assertEqual(task["installation_authorization"], "required")
        self.assertIn("run_preview", task)
        self.app.cancel(submitted["task_id"])
        after = self.app.store.get("settings", "executor_grants")
        self.assertNotIn("codex-work-pc", after["grants"])
        self.assertNotIn("codex-work-pc", after["pending_device"])

    def test_invalid_adapter_advice_is_the_setup_error(self):
        advice = ("installation inst-x adapter retired-adapter is not in the built-in catalog "
                  "or /data/adapters.json")
        request_id = "setup-adapter-advice"
        self.app.store.put("setup_request", {
            "id": request_id, "host_id": "pc", "bot_id": "bot-main", "work_id": self.work,
            "status": "configuring", "proposal": {"action": "configure"},
        })
        self.app.onboarding.report({
            "id": "cmd-advice", "action": "configure", "host_id": "pc",
            "payload": {"request_id": request_id},
        }, {"error": "invalid_adapter", "advice": " \n" + advice + " \n"})
        self.assertEqual(self.app.store.get("setup_request", request_id)["status"], "failed")
        noted = [m["content"] for m in self.app.store.find("message", work_id=self.work)
                 if m.get("kind") == "setup_result"]
        self.assertEqual(noted[-1]["error"], "invalid_adapter: " + advice)

        blank = "setup-blank-advice"
        self.app.store.put("setup_request", {
            "id": blank, "host_id": "pc", "bot_id": "bot-main", "work_id": self.work,
            "status": "configuring", "proposal": {"action": "configure"},
        })
        self.app.onboarding.report({
            "id": "cmd-blank", "action": "configure", "host_id": "pc",
            "payload": {"request_id": blank},
        }, {"error": "invalid_adapter", "advice": "   "})
        self.assertEqual(self.app.store.get("setup_request", blank)["status"], "failed")

        other = "setup-other-error"
        self.app.store.put("setup_request", {
            "id": other, "host_id": "pc", "bot_id": "bot-main", "work_id": self.work,
            "status": "configuring", "proposal": {"action": "configure"},
        })
        self.app.onboarding.report({
            "id": "cmd-other", "action": "configure", "host_id": "pc",
            "payload": {"request_id": other},
        }, {"error": "cli_not_installed", "advice": "ignore this advice " + ("x" * 50)})
        long_id = "setup-long-advice"
        self.app.store.put("setup_request", {
            "id": long_id, "host_id": "pc", "bot_id": "bot-main", "work_id": self.work,
            "status": "configuring", "proposal": {"action": "configure"},
        })
        self.app.onboarding.report({
            "id": "cmd-long", "action": "configure", "host_id": "pc",
            "payload": {"request_id": long_id},
        }, {"error": "invalid_adapter", "advice": "a" * 600})
        contents = [m["content"] for m in self.app.store.find("message", work_id=self.work)
                    if m.get("kind") == "setup_result"]
        by_action = {m["error"]: m for m in contents if m.get("error")}
        self.assertEqual(by_action["invalid_adapter: " + advice]["error"], "invalid_adapter: " + advice)
        self.assertIn("device_setup_failed", by_action)
        self.assertEqual(by_action["invalid_adapter: " + ("a" * 500)]["error"], "invalid_adapter: " + ("a" * 500))
        self.assertFalse(any("ignore this advice" in (m.get("error") or "") for m in contents))
        self.assertNotIn("codex-notes-pc", self.app.execution.installations)


class ManifestTests(unittest.TestCase):
    def test_oversize_command_and_roots_are_marked_truncated(self):
        from pangpang.connector import execution_manifest, sanitize_manifest
        long = "x" * 501
        manifest = execution_manifest({
            "installations": [{"id": "i", "host_id": "h", "argv": [long, "echo"],
                               "extra_argv": [], "cwd": "/w", "secret_refs": {}}],
            "hosts": [{"id": "h", "allowed_roots": ["/r"] * 33}],
        }, "i")
        self.assertIs(manifest["truncated"], True)
        self.assertEqual(manifest["command"][0], "x" * 500)
        self.assertEqual(len(manifest["allowed_roots"]), 32)
        kept = sanitize_manifest(manifest)
        self.assertIs(kept["truncated"], True)
        self.assertEqual(kept["command"], manifest["command"])
        self.assertEqual(kept["allowed_roots"], manifest["allowed_roots"])
        blanked = sanitize_manifest({
            "command": ["y"] * 33, "cwd": "/w", "allowed_roots": ["/w"], "credential_ids": []})
        self.assertIs(blanked["truncated"], True)
        self.assertEqual(blanked["command"], [])
        self.assertEqual(blanked["allowed_roots"], ["/w"])
        plain = sanitize_manifest({"command": ["echo"], "cwd": "/w", "allowed_roots": ["/w"]})
        self.assertNotIn("truncated", plain)


if __name__ == "__main__":
    unittest.main()
