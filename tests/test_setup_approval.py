"""Setup approval keeps unrelated unknown adapters, then probes the new executor."""
import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from pangpang.execution import ADAPTER_CATALOG, Execution
from pangpang.http import serve
from pangpang.onboarding import empty_config
from pangpang.service import Service
from pangpang.store import Fault, Store
from test_bot_runtime import config


UNKNOWN = "retired-adapter"


def _settings_blob(db_path):
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT data FROM objects WHERE kind='settings' AND id='managed'").fetchone()
    finally:
        connection.close()
    return row[0]


def _unknown_row(blob):
    data = json.loads(blob)
    return next(row for row in data["config"]["installations"] if row["adapter"] == UNKNOWN)


class SetupApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.data = self.root.parent / "data"
        self.data.mkdir()
        stored = config(self.root)
        stored["installations"].append({
            "id": "inst-local-" + UNKNOWN,
            "host_id": "local",
            "adapter": UNKNOWN,
            "argv": ["/usr/bin/" + UNKNOWN, "-p"],
            "cwd": str(self.root),
            "enabled": True,
            "prompt_mode": "stdin",
            "risk": "write",
            "timeout_s": 3600,
            "max_concurrency": 2,
            "probe_argv": ["/usr/bin/" + UNKNOWN, "--version"],
        })
        store = Store(self.data / "bots.db")
        store.put("settings", {"id": "managed", "config": stored})
        store.close()
        self.app = Service(empty_config(), self.data, deployment_level="L0")
        self.addCleanup(self.app.store.close)
        self.work = self.app.send("bot-main", {"text": "setup", "allow_write": True}, "setup")["work_id"]
        self.unknown_id = "inst-local-" + UNKNOWN
        self.original = _unknown_row(_settings_blob(self.data / "bots.db"))
        self.payloads = []
        real_run = subprocess.run

        def record(argv, input=None, **kwargs):
            self.payloads.append(input)
            return real_run(argv, input=input, **kwargs)

        self.runner = patch("pangpang.execution.subprocess.run", side_effect=record)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    def _unknown_untouched(self):
        self.assertEqual(_unknown_row(_settings_blob(self.data / "bots.db")), self.original)
        self.assertIn(self.unknown_id, self.app.execution.unknown_adapters)
        self.assertNotIn(self.unknown_id, (self.app.store.get("settings", "executor_grants").get("grants") or {}))
        blob = b"".join(payload for payload in self.payloads if isinstance(payload, bytes))
        self.assertNotIn(self.unknown_id.encode(), blob)
        self.assertNotIn(UNKNOWN.encode(), blob)
        with self.assertRaises(Fault) as missing:
            self.app.store.get("probe", self.unknown_id)
        self.assertEqual(missing.exception.status, 404)

    def test_valid_setup_ignores_an_unrelated_unknown_row_and_probes_the_new_executor(self):
        self.assertNotIn(UNKNOWN, ADAPTER_CATALOG)
        staged = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-local", "extra_argv": ["--strict-config"]})
        updated = self.app.onboarding.approve(staged["staged"]["request_id"], {})
        self.assertTrue(updated["accepted"])
        self.assertEqual(self.app.execution.installations["codex-local"]["extra_argv"], ["--strict-config"])
        self.assertTrue(updated["probe"]["probed"])
        self.assertNotIn("error", updated)
        self._unknown_untouched()

        created = self.app.propose_executor("bot-main", self.work, {
            "host_id": "local", "adapter": "codex", "profile": "notes", "cwd": str(self.root)})
        approved = self.app.onboarding.approve(created["staged"]["request_id"], {"endpoint": ""})
        iid = approved["installation_id"]
        self.assertEqual(iid, "codex-notes-local")
        self.assertTrue(approved["probe"]["available"], approved["probe"])
        self.assertTrue(approved["probe"]["probed"])
        self.assertEqual(self.app.store.get("setup_request", created["staged"]["request_id"])["status"], "done")
        self.assertNotIn(iid, self.app.store.get("settings", "executor_grants")["grants"])
        stored_probe = self.app.store.get("probe", iid)
        self.assertTrue(stored_probe["available"])
        route = self.app.route("bot-main", {"id": self.work}, {"installation_id": iid})
        self.assertEqual(route["selected"]["installation_id"], iid)
        submitted = self.app.submit_task("bot-main", self.work, {"prompt": "go", "installation_id": iid})
        self.assertEqual(submitted["status"], "awaiting_approval")
        self.app.cancel(submitted["task_id"])
        noted = [m["content"] for m in self.app.store.find("message", work_id=self.work)
                 if m.get("kind") == "setup_result" and m["content"].get("installation_id") == iid]
        self.assertEqual(len(noted), 1)
        self.assertTrue(noted[0]["accepted"])
        self.assertTrue(noted[0]["probe"]["available"])
        self.assertNotIn("error", noted[0])
        self._unknown_untouched()
        self.assertFalse((self.root / "result.txt").exists())

    def _grant(self, installation_id):
        return (self.app.store.get("settings", "executor_grants").get("grants") or {}).get(installation_id)

    def test_setup_approval_does_not_grant_the_first_run(self):
        before = dict(self._grant("codex-local"))
        self.assertEqual(before["origin"], "migration")
        staged = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-local", "extra_argv": ["--strict-config"]})
        approved = self.app.onboarding.approve(staged["staged"]["request_id"], {})
        self.assertTrue(approved["accepted"])
        self.assertEqual(self._grant("codex-local"), before)
        first = self.app.submit_task("bot-main", self.work, {
            "prompt": "go", "installation_id": "codex-local"})
        self.assertEqual(first["status"], "awaiting_approval")
        task = self.app.store.get("task", first["task_id"])
        self.assertEqual(task["installation_authorization"], "required")
        self.assertIn("--strict-config", task["run_preview"]["command"])
        self.app.approve(task["id"], {"spec_digest": task["spec_digest"]})
        self.assertEqual(self._grant("codex-local"), before)
        self.app.cancel(task["id"])
        asked = self.app.submit_task("bot-main", self.work, {
            "prompt": "again", "installation_id": "codex-local"})
        self.assertEqual(asked["status"], "awaiting_approval")
        task = self.app.store.get("task", asked["task_id"])
        self.app.approve(task["id"], {"spec_digest": task["spec_digest"], "always": True})
        grant = self._grant("codex-local")
        self.assertEqual(grant["origin"], "operator")
        self.assertEqual(grant["digest"], self.app._authorization_digest("codex-local"))
        self.app.cancel(task["id"])
        held = self.app.submit_task("bot-main", self.work, {
            "prompt": "held", "installation_id": "codex-local"})
        self.assertEqual(held["status"], "queued")
        self.assertNotIn("installation_authorization", self.app.store.get("task", held["task_id"]))
        self.app.cancel(held["task_id"])
        standing = dict(self._grant("codex-local"))
        changed = self.app.propose_executor("bot-main", self.work, {
            "installation_id": "codex-local", "extra_argv": ["--enable", "feature"]})
        self.app.onboarding.approve(changed["staged"]["request_id"], {})
        self.assertEqual(self._grant("codex-local"), standing)
        waited = self.app.submit_task("bot-main", self.work, {
            "prompt": "changed", "installation_id": "codex-local"})
        self.assertEqual(waited["status"], "awaiting_approval")
        self.app.cancel(waited["task_id"])
        self._unknown_untouched()

    def test_probe_failure_stays_a_probe_result(self):
        def fail(self, installation_id):
            raise Fault("transport_unavailable")

        with patch.object(Execution, "probe", side_effect=fail, autospec=True):
            staged = self.app.propose_executor("bot-main", self.work, {
                "host_id": "local", "adapter": "codex", "profile": "plain", "cwd": str(self.root)})
            approved = self.app.onboarding.approve(staged["staged"]["request_id"], {"endpoint": ""})
        iid = approved["installation_id"]
        self.assertTrue(approved["accepted"])
        self.assertFalse(approved["probe"]["available"])
        self.assertEqual(approved["probe"]["reason"], "transport_unavailable")
        self.assertTrue(approved["probe"]["probed"])
        self.assertEqual(self.app.store.get("setup_request", staged["staged"]["request_id"])["status"], "done")
        self.assertIn(iid, self.app.execution.installations)
        stored = self.app.store.get("probe", iid)
        self.assertFalse(stored["available"])
        self.assertEqual(stored["reason"], "transport_unavailable")
        route = self.app.route("bot-main", {"id": self.work}, {"installation_id": iid})
        self.assertIsNone(route["selected"])
        self.assertEqual(
            {row["installation_id"]: row["reason"] for row in route["rejected"]}[iid],
            "discovery_failed")
        noted = [m["content"] for m in self.app.store.find("message", work_id=self.work)
                 if m.get("kind") == "setup_result" and m["content"].get("installation_id") == iid]
        self.assertEqual(noted[0]["accepted"], True)
        self.assertEqual(noted[0]["probe"]["reason"], "transport_unavailable")
        self.assertNotIn("error", noted[0])
        self._unknown_untouched()

    def test_requested_unknown_adapter_is_advice_not_a_bare_code(self):
        request_id = "setup-retired"
        self.app.store.put("setup_request", {
            "id": request_id, "host_id": "local", "bot_id": "bot-main", "work_id": self.work,
            "origin": "executor_proposal", "status": "needs_approval",
            "expires": time.time() + 3600,
            "proposal": {
                "action": "executor_config", "installation_id": self.unknown_id,
                "adapter": UNKNOWN, "draft": {"extra_argv": ["--strict-config"]},
                "base_revision": self.app.config_revision,
            },
        })
        with self.assertRaises(Fault) as raised:
            self.app.onboarding.approve(request_id, {})
        self.assertEqual(raised.exception.code, "invalid_adapter")
        self.assertIn(self.unknown_id, raised.exception.advice)
        self.assertIn("adapters.json", raised.exception.advice)
        self.assertEqual(self.app.store.get("setup_request", request_id)["status"], "needs_approval")
        self._unknown_untouched()

        token = "t" * 24
        server = serve(self.app, token, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = "http://127.0.0.1:" + str(server.server_port)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

        def post(path, body, key):
            req = urllib.request.Request(
                origin + "/api/v1/" + path, data=json.dumps(body).encode(),
                headers={"Origin": origin, "Content-Type": "application/json", "Idempotency-Key": key})
            return opener.open(req)

        try:
            with post("login", {"token": token}, "login"):
                pass
            with self.assertRaises(urllib.error.HTTPError) as caught:
                post("setup/" + request_id + "/approve", {}, "approve-retired")
            payload = json.loads(caught.exception.read().decode())
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(payload["code"], "invalid_adapter")
            self.assertEqual(payload["message"], "invalid_adapter: " + payload["advice"])
            self.assertIn(self.unknown_id, payload["advice"])
            self.assertNotEqual(payload["message"], payload["code"])
            self.assertEqual(self.app.store.get("setup_request", request_id)["status"], "needs_approval")
            self._unknown_untouched()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
