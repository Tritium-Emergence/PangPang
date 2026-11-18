"""The chosen executor writes the dream. The server only stages and checks it."""
import json
import sys
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

_DRAIN = """
import pathlib, sys
sys.stdin.read()
root = pathlib.Path.cwd()
bits = []
signal = root / "signal"
if signal.is_dir():
    for path in sorted(signal.iterdir()):
        if path.is_file():
            bits.append(path.read_text(encoding="utf-8"))
body = "\\n".join(bits)
cand = root / "candidate"
(cand / "topics").mkdir(parents=True, exist_ok=True)
(cand / "MEMORY.md").write_text("# Memory\\nDREAMED\\n" + body, encoding="utf-8")
(cand / "topics" / "note.md").write_text(body, encoding="utf-8")
"""

_FAIL = """
import sys
sys.stdin.read()
sys.exit(1)
"""

_SYMLINK = """
import pathlib, sys
sys.stdin.read()
cand = pathlib.Path("candidate")
(cand / "topics").mkdir(parents=True, exist_ok=True)
(cand / "MEMORY.md").write_text("# Memory\\nDREAMED\\n", encoding="utf-8")
(cand / "topics" / "leak.md").symlink_to("/etc/passwd")
"""

_ESCAPE = """
import pathlib, sys
sys.stdin.read()
cand = pathlib.Path("candidate")
cand.mkdir(parents=True, exist_ok=True)
(cand / "MEMORY.md").write_text("# Memory\\nDREAMED\\n", encoding="utf-8")
pathlib.Path("outside.md").write_text("escaped", encoding="utf-8")
"""

_HUGE = """
import pathlib, sys
sys.stdin.read()
cand = pathlib.Path("candidate")
cand.mkdir(parents=True, exist_ok=True)
(cand / "MEMORY.md").write_text("DREAMED\\n" + ("x" * 30000), encoding="utf-8")
"""


class DreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.data = self.root.parent / "data"
        self.cfg = config(self.root)
        self.app = Service(self.cfg, self.data)
        self.app.probe("codex-local")
        self._argv(_DRAIN)

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _argv(self, code):
        inst = self.app.execution.installations["codex-local"]
        inst["argv"] = [sys.executable, "-c", code]
        # The operator already approved this executor. Swapping the test command
        # has to refresh that grant or the task waits for approval.
        self.app._put_executor_grant(inst["id"], self.app._captured_authorization_digest(inst), "test")

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

    def _drain(self):
        deadline = time.time() + 20
        while time.time() < deadline:
            self.app.task_step()
            live = [task for task in self.app.store.find("task", limit=100)
                    if task.get("dreaming") and task.get("status") not in {"succeeded", "failed", "cancelled"}]
            if not live:
                return
            time.sleep(0.05)
        self.fail("dreaming task did not finish")

    def _fire(self):
        slot = self.app.store.get("settings", "dreaming")["armed_slot"]
        self.app.dream_step(slot)
        self._drain()
        return slot

    def _memory_text(self, project_id):
        root = self.data / "memory" / project_id
        chunks = []
        if not root.exists():
            return ""
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file() or path.suffix == ".tmp":
                continue
            chunks.append(path.read_text(encoding="utf-8"))
        return "\n".join(chunks)

    def test_settings_persist_and_accept_one_executor(self):
        view = self._enable("03:30")
        self.assertEqual(view["time"], "03:30")
        self.assertTrue(view["enabled"])
        self.assertEqual(view["installation_id"], "codex-local")
        self.assertEqual(view["timezone"], "Asia/Shanghai")
        self.assertTrue(any(item["id"] == "codex-local" and item["local"] for item in view["executors"]))
        host = self.app.execution.hosts["local"]
        host["transport"] = "connector"
        try:
            with self.assertRaises(Fault):
                self.app.set_dreaming({
                    "enabled": True, "installation_id": "codex-local", "time": "02:00",
                })
        finally:
            host["transport"] = "local"
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

    def test_executor_publishes_only_projects_with_new_material(self):
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
        self.assertIn("DREAMED", text)
        self.assertIn("ALPHA-SUMMARY", text)
        self.assertIn("receipt tail", text)
        self.assertIn("note.txt", text)
        self.assertNotIn("SESSION-SECRET-BYTES", text)
        self.assertNotIn(str(secret), text)
        self.assertNotIn("DREAMED", dreaming.PROMPT)
        self.assertFalse((self.data / "memory" / empty["id"] / "CURRENT").exists())
        self.assertFalse((self.root / "MEMORY.md").exists())
        self.assertEqual(len(self.app.store.find("notification")), notes)
        self.assertIsNotNone(self.app.store.get("project", alpha["id"]).get("dreamed_at"))
        self.assertNotIn("dream_alert", self.app.store.get("project", alpha["id"]))
        watermark = dreaming.formal_watermark(self.app, alpha["id"])
        dreaming_tasks = [task for task in self.app.store.find("task", limit=100) if task.get("dreaming")]
        self.assertEqual(len(dreaming_tasks), 1)
        self.assertEqual(dreaming_tasks[0]["status"], "succeeded")
        self._fire()
        self.assertEqual(dreaming.formal_watermark(self.app, alpha["id"]), watermark)
        self.assertEqual(len([task for task in self.app.store.find("task", limit=100) if task.get("dreaming")]), 1)

    def test_executor_failure_keeps_memory(self):
        self._enable()
        alpha, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        self._argv(_FAIL)
        self._fire()
        self.assertEqual(self._memory_text(alpha["id"]), "")
        self.assertIsNone(dreaming.formal_watermark(self.app, alpha["id"]))
        alert = self.app.store.get("project", alpha["id"])["dream_alert"]
        self.assertEqual(alert["kind"], "failed")
        self.assertIn("执行器", alert["text"])
        self.assertNotIn("{", alert["text"])

    def test_symlink_and_escape_and_oversize_are_rejected(self):
        self._enable()
        alpha, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        for code, needle in ((_SYMLINK, "符号链接"), (_ESCAPE, "外面"), (_HUGE, "大小")):
            self._argv(code)
            before = dreaming.formal_watermark(self.app, alpha["id"])
            self._fire()
            self.assertEqual(dreaming.formal_watermark(self.app, alpha["id"]), before)
            self.assertNotIn("DREAMED", self._memory_text(alpha["id"]))
            self.assertIn(needle, self.app.store.get("project", alpha["id"])["dream_alert"]["text"])

    def test_crash_before_publish_resumes_from_the_same_watermark(self):
        self._enable()
        alpha, work_id = self._project("Alpha", "OLD-SUMMARY", "alpha")
        self._fire()
        watermark = dreaming.formal_watermark(self.app, alpha["id"])
        index = dreaming.formal_index(self.app, alpha["id"])
        self.assertIn("DREAMED", index)
        self.assertIn("OLD-SUMMARY", index)
        self.app.checkpoint_work("bot-main", work_id, {"summary": "NEW-SUMMARY", "phase": "build"})

        def stop(_project):
            raise RuntimeError("stop")

        self.app.dream_before_commit = stop
        self._fire()
        self.assertEqual(dreaming.formal_index(self.app, alpha["id"]), index)
        self.assertEqual(dreaming.formal_watermark(self.app, alpha["id"]), watermark)
        self.assertNotIn("NEW-SUMMARY", dreaming.formal_index(self.app, alpha["id"]))
        self.assertIn("中断", self.app.store.get("project", alpha["id"])["dream_alert"]["text"])
        self.app.dream_before_commit = None
        self._fire()
        self.assertIn("NEW-SUMMARY", dreaming.formal_index(self.app, alpha["id"]))
        self.assertIn("DREAMED", dreaming.formal_index(self.app, alpha["id"]))
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
        self.assertIn("DREAMED", prompt)
        self.assertIn("ALPHA-SUMMARY", prompt)
        self.assertIn("Work progress", prompt)
        other = self.app.send("bot-main", {"text": "unrelated"}, "other")
        other_task = self.app.submit_task("bot-main", other["work_id"], {"prompt": "something else"})
        other_prompt = self.app.store.get("task", other_task["task_id"])["prompt"]
        self.assertNotIn("DREAMED", other_prompt)
        self.assertNotIn(dreaming.INDEX_PROMPT_LABEL, other_prompt)

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
            self.assertFalse(any(task.get("dreaming") for task in restarted.store.find("task", limit=50)))
        finally:
            restarted.store.close()
        self.app = Service(self.cfg, self.data)
        self.app.probe("codex-local")
        self._argv(_DRAIN)
        self.app.store.put("task", {
            "id": "task-busy", "status": "running", "installation_id": "codex-local",
            "host_id": "local", "work_id": "work-busy", "owner_bot_id": "bot-main",
            "created": time.time(),
        })
        slot = self.app.store.get("settings", "dreaming")["armed_slot"]
        self.app.dream_step(slot)
        self.assertEqual(self.app.store.get("task", "task-busy")["status"], "running")
        self.assertFalse(any(task.get("dreaming") for task in self.app.store.find("task", limit=50)))
        self.assertIsNotNone(self.app.store.get("settings", "dreaming").get("pending_slot"))
        busy = self.app.store.get("task", "task-busy")
        busy["status"] = "succeeded"
        self.app.store.put("task", busy)
        self.app.dream_step(time.time())
        self._drain()
        self.assertTrue(any(task.get("dreaming") and task.get("status") == "succeeded"
                            for task in self.app.store.find("task", limit=50)))
        self.assertIn("DREAMED", dreaming.formal_index(self.app, alpha["id"]))
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
            self.assertTrue(saved["executors"][0]["local"])
            with request("snapshot") as response:
                snap = json.loads(response.read().decode())
            self.assertEqual(snap["dreaming"]["installation_id"], "codex-local")
            self.assertEqual(snap["dreaming"]["timezone"], "Asia/Shanghai")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
