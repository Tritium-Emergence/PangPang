"""Dreaming: one daily pass per project over material already stored on the server."""
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from test_bot_runtime import config

from pangpang import dreaming
from pangpang.http import serve
from pangpang.service import Service
from pangpang.store import Fault


class DreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.data = self.root.parent / "data"
        self.cfg = config(self.root)
        self.app = Service(self.cfg, self.data)
        self.app.probe("codex-local")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _enable(self, clock="02:00"):
        return self.app.set_dreaming({
            "enabled": True, "installation_id": "codex-local", "time": clock,
        })

    def _project(self, title, summary=None, key="p"):
        project = self.app.create_project("bot-main", {"title": title, "goal": title + " goal"})
        message = self.app.send("bot-main", {"text": title + " work", "project_id": project["id"]}, key)
        if summary is not None:
            self.app.checkpoint_work("bot-main", message["work_id"], {
                "phase": "build", "summary": summary, "completed": ["noted"],
                "next_actions": ["continue"], "blocked_on": [],
            })
        return project, message["work_id"]

    def _fire(self):
        slot = self.app.store.get("settings", "dreaming")["armed_slot"]
        self.app.dream_step(slot)
        self.app.task_step()
        return slot

    def _memory_text(self, project_id):
        root = self.data / "memory" / project_id
        chunks = []
        if not root.exists():
            return ""
        for path in root.rglob("*"):
            if path.is_file() and path.suffix != ".tmp":
                chunks.append(path.read_text(encoding="utf-8"))
        return "\n".join(chunks)

    def test_settings_persist_and_accept_one_executor(self):
        view = self._enable("03:30")
        self.assertEqual(view, {
            "enabled": True, "installation_id": "codex-local",
            "time": "03:30", "timezone": "Asia/Shanghai",
        })
        with self.assertRaises(Fault):
            self.app.set_dreaming({
                "enabled": True,
                "installation_id": ["codex-local", "other-executor"],
                "time": "02:00",
            })
        with self.assertRaises(Fault):
            self.app.set_dreaming({"enabled": True, "installation_id": "missing", "time": "02:00"})
        with self.assertRaises(Fault):
            self.app.set_dreaming({"enabled": True, "installation_id": "codex-local", "time": "25:99"})
        self.app.store.close()
        again = Service(self.cfg, self.data)
        try:
            self.assertEqual(again.dreaming_view()["time"], "03:30")
            self.assertTrue(again.dreaming_view()["enabled"])
            self.assertEqual(again.dreaming_view()["installation_id"], "codex-local")
            self.assertEqual(again.dreaming_view()["timezone"], "Asia/Shanghai")
            self.assertGreater(again.store.get("settings", "dreaming")["armed_slot"], time.time() - 1)
        finally:
            again.store.close()
        self.app = Service(self.cfg, self.data)

    def test_scheduled_run_skips_projects_without_new_material(self):
        secret = self.root / "session.jsonl"
        secret.write_text("SESSION-SECRET-BYTES", encoding="utf-8")
        self._enable()
        alpha, work_id = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        self.app.store.put("artifact", {
            "id": "artifact-alpha", "path": str(secret), "digest": "ab" * 32, "size": 20,
            "host_id": "local", "owner_bot_id": "bot-main", "work_id": work_id,
            "source": "recorded-task", "name": "note.txt",
        })
        self.app.store.put("task", {
            "id": "task-alpha-receipt", "work_id": work_id, "owner_bot_id": "bot-main",
            "status": "succeeded", "installation_id": "codex-local", "host_id": "local",
            "receipt": {"status": "succeeded", "tail": "receipt tail"},
            "created": time.time(),
        })
        empty = self.app.create_project("bot-main", {"title": "Empty", "goal": "nothing yet"})
        notes = len(self.app.store.find("notification"))
        self._fire()
        text = self._memory_text(alpha["id"])
        self.assertIn("ALPHA-SUMMARY", text)
        self.assertIn("receipt tail", text)
        self.assertIn("note.txt", text)
        self.assertNotIn("SESSION-SECRET-BYTES", text)
        self.assertNotIn(str(secret), text)
        self.assertFalse((self.data / "memory" / empty["id"] / "CURRENT").exists())
        self.assertFalse((self.root / "index.md").exists())
        self.assertEqual(len(self.app.store.find("notification")), notes)
        self.assertIsNotNone(self.app.store.get("project", alpha["id"]).get("dreamed_at"))
        self.assertNotIn("dream_alert", self.app.store.get("project", alpha["id"]))
        watermark = dreaming.formal_watermark(self.app, alpha["id"])
        dreaming_tasks = [t for t in self.app.store.find("task", limit=100) if t.get("dreaming")]
        self.assertEqual(len(dreaming_tasks), 1)
        self._fire()
        self.assertEqual(dreaming.formal_watermark(self.app, alpha["id"]), watermark)
        self.assertEqual(len([t for t in self.app.store.find("task", limit=100) if t.get("dreaming")]), 1)

    def test_interruption_keeps_memory_and_watermark(self):
        self._enable()
        alpha, work_id = self._project("Alpha", "OLD-SUMMARY", "alpha")
        self._fire()
        watermark = dreaming.formal_watermark(self.app, alpha["id"])
        index = dreaming.formal_index(self.app, alpha["id"])
        self.assertIn("OLD-SUMMARY", index)
        self.app.checkpoint_work("bot-main", work_id, {"summary": "NEW-SUMMARY", "phase": "build"})
        def stop(_project):
            raise RuntimeError("stop")
        self.app.dream_before_commit = stop
        notes = len(self.app.store.find("notification"))
        self._fire()
        self.assertEqual(dreaming.formal_index(self.app, alpha["id"]), index)
        self.assertEqual(dreaming.formal_watermark(self.app, alpha["id"]), watermark)
        self.assertNotIn("NEW-SUMMARY", dreaming.formal_index(self.app, alpha["id"]))
        self.assertEqual(self.app.store.get("project", alpha["id"])["dream_alert"]["kind"], "failed")
        self.assertEqual(len(self.app.store.find("notification")), notes)
        self.app.dream_before_commit = None
        self._fire()
        self.assertIn("NEW-SUMMARY", dreaming.formal_index(self.app, alpha["id"]))
        self.assertNotEqual(dreaming.formal_watermark(self.app, alpha["id"]), watermark)
        self.assertNotIn("dream_alert", self.app.store.get("project", alpha["id"]))
        revised = self.app.store.get("work", work_id)["revision"]
        self.assertEqual(dreaming.formal_watermark(self.app, alpha["id"])["work:" + work_id], revised)

    def test_next_task_prompt_includes_the_index(self):
        self._enable()
        alpha, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        self._fire()
        followed = self.app.send("bot-main", {"text": "continue alpha", "project_id": alpha["id"]}, "follow")
        submitted = self.app.submit_task("bot-main", followed["work_id"], {"prompt": "continue the work"})
        prompt = self.app.store.get("task", submitted["task_id"])["prompt"]
        self.assertIn(dreaming.INDEX_PROMPT_LABEL, prompt)
        self.assertIn("ALPHA-SUMMARY", prompt)
        self.assertIn("Work progress", prompt)
        other = self.app.send("bot-main", {"text": "unrelated"}, "other")
        other_task = self.app.submit_task("bot-main", other["work_id"], {"prompt": "something else"})
        other_prompt = self.app.store.get("task", other_task["task_id"])["prompt"]
        self.assertNotIn("ALPHA-SUMMARY", other_prompt)
        self.assertNotIn(dreaming.INDEX_PROMPT_LABEL, other_prompt)

    def test_out_of_scope_paths_are_refused(self):
        project, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        root = dreaming.memory_root(self.app, project["id"])
        gen = "gen-" + ("a" * 32)
        write_root = root / "g" / gen
        write_root.mkdir(parents=True)
        scope = dreaming.Scope([], write_root)
        secret = self.root / "session.jsonl"
        secret.write_text("SESSION-SECRET-BYTES", encoding="utf-8")
        with self.assertRaises(Fault) as refused:
            scope.read(secret)
        self.assertEqual(refused.exception.code, "dream_scope_refused")
        with self.assertRaises(Fault):
            scope.write(self.root / "pangpang-product.py", "no")
        with self.assertRaises(Fault):
            scope.write(self.data / "bots.db", "no")
        other = dreaming.memory_root(self.app, "project-" + ("b" * 32))
        with self.assertRaises(Fault):
            scope.write(other / "index.md", "no")
        scope.write(write_root / "index.md", "only this file\n")
        self.assertEqual(dreaming.Scope([write_root], None).read(write_root / "index.md"), "only this file\n")
        with self.assertRaises(Fault):
            dreaming.memory_root(self.app, "../etc")
        self.assertFalse((self.root / "pangpang-product.py").exists())

    def test_boot_does_not_catch_up_and_busy_executor_is_not_preempted(self):
        self._enable()
        alpha, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        row = self.app.store.get("settings", "dreaming")
        row["pending_slot"] = time.time() - 86400 * 3
        row["armed_slot"] = time.time() - 3600
        self.app.store.put("settings", row)
        self.app.store.close()
        restarted = Service(self.cfg, self.data)
        try:
            stored = restarted.store.get("settings", "dreaming")
            self.assertIsNone(stored.get("pending_slot"))
            self.assertGreater(stored["armed_slot"], time.time() - 1)
            self.assertFalse((self.data / "memory" / alpha["id"] / "CURRENT").exists())
            self.assertFalse(restarted.dream_step(time.time()))
            self.assertFalse(any(t.get("dreaming") for t in restarted.store.find("task", limit=50)))
        finally:
            restarted.store.close()
        self.app = Service(self.cfg, self.data)
        self.app.probe("codex-local")
        self.app.store.put("task", {
            "id": "task-busy", "status": "running", "installation_id": "codex-local",
            "host_id": "local", "work_id": "work-busy", "owner_bot_id": "bot-main",
            "created": time.time(),
        })
        slot = self.app.store.get("settings", "dreaming")["armed_slot"]
        self.app.dream_step(slot)
        self.assertEqual(self.app.store.get("task", "task-busy")["status"], "running")
        self.assertFalse(any(t.get("dreaming") for t in self.app.store.find("task", limit=50)))
        self.assertIsNotNone(self.app.store.get("settings", "dreaming").get("pending_slot"))
        busy = self.app.store.get("task", "task-busy")
        busy["status"] = "succeeded"
        self.app.store.put("task", busy)
        self.app.dream_step(time.time())
        self.app.task_step()
        self.assertTrue(any(t.get("dreaming") and t.get("status") == "succeeded"
                            for t in self.app.store.find("task", limit=50)))
        self.assertIn("ALPHA-SUMMARY", dreaming.formal_index(self.app, alpha["id"]))

    def test_http_form_fields_roundtrip(self):
        token = "d" * 32
        server = serve(self.app, token, port=0)
        origin = "http://127.0.0.1:" + str(server.server_port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        jar = CookieJar()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def request(path, body=None, key="dream-http"):
            headers = {"Origin": origin}
            data = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                headers["Idempotency-Key"] = key
                data = json.dumps(body).encode()
            return client.open(urllib.request.Request(origin + "/api/v1/" + path, data=data, headers=headers))

        try:
            with request("login", {"token": token}, key="login"):
                pass
            with request("dreaming", {
                "enabled": True, "installation_id": "codex-local", "time": "04:15",
            }, key="save-dream") as response:
                saved = json.loads(response.read().decode())
            self.assertEqual(saved["time"], "04:15")
            self.assertTrue(saved["enabled"])
            self.assertNotIn("armed_slot", saved)
            self.assertNotIn("pending_slot", saved)
            with request("snapshot") as response:
                snap = json.loads(response.read().decode())
            self.assertEqual(snap["dreaming"]["installation_id"], "codex-local")
            self.assertEqual(snap["dreaming"]["timezone"], "Asia/Shanghai")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
