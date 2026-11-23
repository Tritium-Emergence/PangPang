"""The chosen executor writes the dream. The server only stages and checks it."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from unittest import mock

from test_bot_runtime import config

from pangpang import dreaming
from pangpang.http import serve
from pangpang.service import Service
from pangpang.store import Fault, uid

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
        # Each call is its own scheduled slot. A failure in one slot must not
        # be started again until the next time the clock is due.
        row = self.app.store.get("settings", "dreaming")
        row["pending_slot"] = None
        row["attempted"] = []
        row["cooled"] = []
        row["retry_not_before"] = None
        row["armed_slot"] = time.time() - 1
        self.app.store.put("settings", row)
        self.app.dream_step(time.time())
        self._drain()
        return row["armed_slot"]

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
        for code, needle in ((_SYMLINK, "符号链接"), (_ESCAPE, "执行器"), (_HUGE, "大小")):
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
        self.assertIn("work:" + work_id, dreaming.formal_watermark(self.app, alpha["id"]))

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


class ReviewTests(unittest.TestCase):
    setUp = DreamingTests.setUp
    tearDown = DreamingTests.tearDown
    _argv = DreamingTests._argv
    _enable = DreamingTests._enable
    _project = DreamingTests._project
    _drain = DreamingTests._drain
    _memory_text = DreamingTests._memory_text

    def _due(self):
        self._enable()
        row = self.app.store.get("settings", "dreaming")
        row["armed_slot"] = time.time() - 5
        self.app.store.put("settings", row)

    def test_restart_does_not_run_an_unbound_dream_in_the_product_directory(self):
        _project, work_id = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        work = self.app.store.get("work", work_id)
        work["kind"] = "dreaming"
        self.app.store.put("work", work)
        # Restart reloads the configured executor. Submit with that same command
        # so the stored grant still matches and the only reason not to start is
        # the missing dream binding.
        self._argv(self.cfg["installations"][0]["argv"][2])
        submitted = self.app.submit_task("bot-main", work_id, {"prompt": "dream"})
        task = self.app.store.get("task", submitted["task_id"])
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["cwd"], str(self.root))
        self.assertNotIn("launch_roots", task)
        self.assertNotIn("dreaming", task)
        self.app.store.close()
        restarted = Service(self.cfg, self.data)
        try:
            restarted.task_step()
            stored = restarted.store.get("task", task["id"])
            self.assertEqual(stored["status"], "failed")
            self.assertEqual(stored["error"], "dream_unbound")
            self.assertFalse((self.root / "result.txt").exists())
        finally:
            restarted.store.close()
        self.app = Service(self.cfg, self.data)
        self.app.probe("codex-local")
        self._argv(_DRAIN)

    def test_the_first_saved_task_is_already_the_dream(self):
        self._due()
        self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        first = {}
        real = self.app.store.put

        def spy(kind, row, expected=None):
            stored = real(kind, row, expected)
            if kind == "task" and row.get("dreaming") and "cwd" not in first:
                first.update(stored)
            return stored

        self.app.store.put = spy
        try:
            self.app.dream_step(time.time())
        finally:
            self.app.store.put = real
        self.assertEqual(first["cwd"], first["dream_stage"])
        self.assertEqual(first["launch_roots"], [first["dream_stage"]])
        self.assertTrue(first.get("dream_cursor"))
        self.assertNotEqual(first["cwd"], str(self.root))
        self.assertIn(first["status"], {"queued", "awaiting_approval"})

    def test_unknown_with_a_live_process_is_not_closed_by_dream_step(self):
        project, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        stage = self.data.resolve().parent / "dream-stage" / project["id"] / "run-live"
        stage.mkdir(parents=True)
        (stage / "candidate").mkdir()
        work = self.app.store.put("work", {
            "id": uid("work"), "owner_bot_id": "bot-main", "goal": "dream", "status": "active",
            "kind": "dreaming", "project_id": project["id"],
            "created": time.time(), "checkpoint": {"phase": "dream"},
        })
        task = self.app.store.put("task", {
            "id": uid("task"), "work_id": work["id"], "status": "unknown", "dreaming": True,
            "dreaming_project_id": project["id"], "dream_stage": str(stage.resolve()),
            "installation_id": "codex-local", "host_id": "local",
            "supervisor": {"supervisor_pid": os.getpid()}, "created": time.time(),
        })
        self._due()
        self.app.dream_step(time.time())
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "unknown")
        self.assertTrue(stage.is_dir())
        self.assertFalse(self.app.store.get("task", task["id"]).get("error"))
        self.assertEqual(len([row for row in self.app.store.find("task", limit=20) if row.get("dreaming")]), 1)

    def test_a_sustained_route_fault_does_not_spin_or_stack_stages(self):
        self._due()
        self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        real = self.app._check_backpressure

        def blocked(_bot_id):
            raise Fault("backpressure", 429)

        self.app._check_backpressure = blocked
        try:
            now = time.time()
            self.assertTrue(self.app.dream_step(now))
            self.assertFalse(self.app.dream_step(now + 1))
            self.assertTrue(self.app.dream_step(now + dreaming.DEFER_S + 1))
        finally:
            self.app._check_backpressure = real
        dreams = [row for row in self.app.store.find("work", limit=20) if row.get("kind") == "dreaming"]
        self.assertEqual(dreams, [])
        self.assertEqual(list((self.data.resolve().parent / "dream-stage").rglob("candidate")), [])

    def test_a_waiting_dream_is_not_failed_by_the_ordinary_queue_timeout(self):
        self._due()
        self._project("One", "ONE", "one")
        self._project("Two", "TWO", "two")
        self.app.execution.hosts["local"]["concurrency"] = 1
        self.app.dream_step(time.time())
        tasks = [row for row in self.app.store.find("task", limit=20) if row.get("dreaming")]
        self.assertEqual(len(tasks), 1)
        self.assertNotIn("queue_deadline", tasks[0])
        tasks[0]["queue_deadline"] = time.time() - 5
        self.app.store.put("task", tasks[0])
        self.app.sweep_deadlines()
        self.assertNotEqual(self.app.store.get("task", tasks[0]["id"])["status"], "failed")
        self.app.dream_step(time.time() + 1)
        self.assertEqual(len([row for row in self.app.store.find("task", limit=20) if row.get("dreaming")]), 1)

    def test_a_hard_link_a_directory_symlink_and_a_replaced_file_are_rejected(self):
        hold = Path(self.tmp.name) / "hold"
        stage = hold / "stage"
        (stage / "candidate" / "topics").mkdir(parents=True)
        target = stage / "candidate" / "MEMORY.md"
        target.write_text("# Memory\n", encoding="utf-8")
        linked = stage / "candidate" / "topics" / "note.md"
        os.link(target, linked)
        with self.assertRaises(dreaming.CandidateRejected) as hard:
            dreaming._candidate_files(stage)
        self.assertIn("外面", str(hard.exception))
        linked.unlink()
        outside = hold / "outside"
        outside.mkdir()
        (stage / "candidate" / "topics" / "gone").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(dreaming.CandidateRejected) as link:
            dreaming._candidate_files(stage)
        self.assertIn("符号链接", str(link.exception))
        (stage / "candidate" / "topics" / "gone").unlink()
        real = os.fstat

        def swapped(fd):
            info = real(fd)
            swapped.calls += 1
            if swapped.calls >= 2:
                values = list(info)
                values[1] = info.st_ino + 1
                return os.stat_result(values)
            return info

        swapped.calls = 0
        with mock.patch("pangpang.dreaming.os.fstat", swapped):
            with self.assertRaises(dreaming.CandidateRejected) as replaced:
                dreaming._read_limited(target, dreaming.INDEX_MAX)
        self.assertIn("外面", str(replaced.exception))

    def test_an_oversized_candidate_is_refused_before_it_is_fully_read(self):
        path = Path(self.tmp.name) / "MEMORY.md"
        path.write_bytes(b"x" * (dreaming.INDEX_MAX + 50))
        seen = {}
        real = os.read

        def limited(fd, size):
            seen["size"] = size
            return real(fd, size)

        with mock.patch("pangpang.dreaming.os.read", limited):
            with self.assertRaises(dreaming.CandidateRejected) as raised:
                dreaming._read_limited(path, dreaming.INDEX_MAX)
        self.assertEqual(seen["size"], dreaming.INDEX_MAX + 1)
        self.assertIn("大小", str(raised.exception))

    def test_a_finished_stage_and_unpublished_generation_are_removed(self):
        self._due()
        alpha, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        self.app.dream_step(time.time())
        self._drain()
        root = dreaming.memory_root(self.app, alpha["id"])
        current = (root / "CURRENT").read_text(encoding="utf-8").strip()
        leftover = root / "g" / ("gen-" + "ab" * 16)
        leftover.mkdir()
        (leftover / "MEMORY.md").write_text("stale\n", encoding="utf-8")
        orphan = self.data.resolve().parent / "dream-stage" / alpha["id"] / "run-orphan"
        orphan.mkdir(parents=True)
        for _ in range(20):
            self.app.dream_step(time.time() + 1)
            if not leftover.exists() and not orphan.exists():
                break
        self.assertTrue((root / "g" / current).is_dir())
        self.assertFalse(leftover.exists())
        self.assertFalse(orphan.exists())
        self.assertEqual(list((self.data.resolve().parent / "dream-stage").rglob("candidate")), [])

    def test_two_publishes_of_one_project_do_not_both_win(self):
        self._due()
        alpha, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        self.app.dream_step(time.time())
        task = next(row for row in self.app.store.find("task", limit=20) if row.get("dreaming"))
        stage = Path(task["dream_stage"])
        (stage / "candidate" / "MEMORY.md").write_text("# Memory\nONE\n", encoding="utf-8")
        (stage / "candidate" / "topics").mkdir(exist_ok=True)
        (stage / "candidate" / "topics" / "note.md").write_text("one\n", encoding="utf-8")
        other = Path(str(stage) + "-other")
        shutil.copytree(stage, other)
        second = dict(task, id=uid("task"), dream_stage=str(other))
        second["dream_cursor"] = dict(task["dream_cursor"], marker="other")
        self.app.store.put("task", second)
        barrier = threading.Barrier(2)
        errors = []

        def publish(row):
            try:
                barrier.wait(timeout=5)
                dreaming.accept_candidate(self.app, row)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=publish, args=(row,)) for row in (task, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        rejected = [error for error in errors if isinstance(error, dreaming.CandidateRejected)]
        self.assertEqual(len(rejected), 1)
        root = dreaming.memory_root(self.app, alpha["id"])
        gens = [path for path in (root / "g").iterdir() if path.is_dir()]
        self.assertEqual(len(gens), 1)
        self.assertEqual((root / "CURRENT").read_text(encoding="utf-8").strip(), gens[0].name)

    def test_a_published_generation_is_recorded_after_the_task_write_fails(self):
        self._due()
        alpha, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        self.app.dream_step(time.time())
        task = next(row for row in self.app.store.find("task", limit=20) if row.get("dreaming"))
        stage = Path(task["dream_stage"])
        (stage / "candidate" / "MEMORY.md").write_text("# Memory\nKEPT\n", encoding="utf-8")
        (stage / "candidate" / "topics").mkdir(exist_ok=True)
        (stage / "candidate" / "topics" / "note.md").write_text("kept\n", encoding="utf-8")
        real = self.app.store.put

        def fail_once(kind, row, expected=None):
            if kind == "task" and row.get("id") == task["id"] and row.get("dream_generation"):
                raise RuntimeError("disk")
            return real(kind, row, expected)

        self.app.store.put = fail_once
        try:
            dreaming.accept_candidate(self.app, task)
        finally:
            self.app.store.put = real
        self.assertIsNone(self.app.store.get("task", task["id"]).get("dream_generation"))
        failed = self.app.store.get("task", task["id"])
        failed["status"] = "failed"
        self.app.store.put("task", failed)
        self.app.dream_step(time.time() + 1)
        self.assertTrue(self.app.store.get("task", task["id"]).get("dream_generation"))
        self.assertIn("KEPT", dreaming.formal_index(self.app, alpha["id"]))
        self.assertIn("KEPT", self._memory_text(alpha["id"]))

    def test_a_bookkeeping_change_is_not_new_material(self):
        project, work_id = self._project("Alpha", "SAME", "alpha")
        works, tasks, artifacts = dreaming._sources(self.app, project["id"])
        cursor = dreaming._cursor(works, tasks, artifacts)
        work = self.app.store.get("work", work_id)
        self.app.store.put("work", dict(work, status="running"))
        works, tasks, artifacts = dreaming._sources(self.app, project["id"])
        self.assertFalse(dreaming._has_new(cursor, works, tasks, artifacts))
        task = self.app.store.put("task", {
            "id": uid("task"), "work_id": work_id, "status": "running", "created": time.time(),
        })
        self.app.store.put("task", dict(task, last_observation_error="poll"))
        works, tasks, artifacts = dreaming._sources(self.app, project["id"])
        self.assertFalse(dreaming._has_new(cursor, works, tasks, artifacts))
        self.assertEqual(dreaming._signal_task(self.app.store.get("task", task["id"])), "")
        self.app.checkpoint_work("bot-main", work_id, {"summary": "CHANGED", "phase": "build"})
        works, tasks, artifacts = dreaming._sources(self.app, project["id"])
        self.assertTrue(dreaming._has_new(cursor, works, tasks, artifacts))
        self.app.store.put("task", dict(self.app.store.get("task", task["id"]),
                                         status="succeeded", receipt={"status": "succeeded", "tail": "done"}))
        works, tasks, artifacts = dreaming._sources(self.app, project["id"])
        self.assertTrue(any(key.startswith("task:") for key in dreaming._cursor(works, tasks, artifacts)))

    def test_a_starting_dream_without_a_supervisor_stays_quarantined(self):
        project, work_id = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        work = self.app.store.get("work", work_id)
        work["kind"] = "dreaming"
        self.app.store.put("work", work)
        execution_id = "exec-" + "ab" * 16
        task = self.app.store.put("task", {
            "id": uid("task"), "work_id": work_id, "status": "starting",
            "host_id": "local", "installation_id": "codex-local", "execution_id": execution_id,
            "cwd": str(self.root), "created": time.time(), "owner_bot_id": "bot-main",
        })
        directory = Path(self.app.execution.directory(self.app.execution.hosts["local"], execution_id))
        directory.mkdir(parents=True)
        (directory / "spec.json").write_text("{}", encoding="utf-8")
        self.app.task_step()
        stored = self.app.store.get("task", task["id"])
        self.assertEqual(stored["status"], "unknown")
        self.assertNotEqual(stored.get("error"), "dream_unbound")
        self.assertFalse((self.root / "result.txt").exists())
        self.assertEqual(self.app.store.get("project", project["id"]).get("id"), project["id"])

    def test_a_failed_project_does_not_take_the_slot_again(self):
        self._due()
        first, _one = self._project("One", "ONE", "one")
        second, _two = self._project("Two", "TWO", "two")
        self.app.dream_step(time.time())
        tasks = [row for row in self.app.store.find("task", limit=20) if row.get("dreaming")]
        self.assertEqual(len(tasks), 1)
        tasks[0]["status"] = "failed"
        self.app.store.put("task", tasks[0])
        self.app.dream_step(time.time() + 1)
        again = [row for row in self.app.store.find("task", limit=20) if row.get("dreaming")]
        self.assertEqual(len(again), 2)
        self.assertEqual(sum(1 for row in again if row.get("dreaming_project_id") == first["id"]), 1)
        self.assertEqual(sum(1 for row in again if row.get("dreaming_project_id") == second["id"]), 1)

    def test_waiting_dreams_are_counted_past_the_first_page(self):
        for _index in range(55):
            self.app.store.put("task", {
                "id": uid("task"), "status": "queued", "installation_id": "codex-local",
                "created": time.time(), "dreaming": False,
            })
        self.app.store.put("task", {
            "id": uid("task"), "status": "queued", "installation_id": "codex-local",
            "created": time.time(), "dreaming": True,
        })
        self.assertEqual(dreaming._dreams_waiting(self.app, "codex-local"), 1)

    def test_a_fifo_is_rejected_without_blocking(self):
        stage = Path(self.tmp.name) / "fifo-stage"
        (stage / "candidate").mkdir(parents=True)
        os.mkfifo(stage / "candidate" / "MEMORY.md")
        started = time.time()
        with self.assertRaises(dreaming.CandidateRejected):
            dreaming._candidate_files(stage)
        self.assertLess(time.time() - started, 2)

    def test_a_rewrite_during_the_read_is_rejected(self):
        path = Path(self.tmp.name) / "MEMORY.md"
        path.write_text("# Memory\n", encoding="utf-8")
        real = os.read

        def rewrite(fd, size):
            with open(path, "r+b") as handle:
                handle.write(b"y" * 32)
                handle.flush()
            return real(fd, size)

        with mock.patch("pangpang.dreaming.os.read", rewrite):
            with self.assertRaises(dreaming.CandidateRejected) as raised:
                dreaming._read_limited(path, dreaming.INDEX_MAX)
        self.assertIn("外面", str(raised.exception))

    def test_sealed_inputs_are_removed_after_the_attempt_ends(self):
        project, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        stage = self.data.resolve().parent / "dream-stage" / project["id"] / "run-sealed"
        signal = stage / "signal"
        signal.mkdir(parents=True)
        (signal / "note.txt").write_text("sealed\n", encoding="utf-8")
        os.chmod(signal / "note.txt", 0o444)
        os.chmod(signal, 0o555)
        os.chmod(stage, 0o755)
        self.app.store.put("task", {
            "id": uid("task"), "status": "failed", "dreaming": True,
            "dreaming_project_id": project["id"], "dream_stage": str(stage.resolve()),
            "created": time.time(),
        })
        self.app.dream_step(time.time())
        self.assertFalse(stage.exists())

    def test_a_cancelled_attempt_does_not_publish(self):
        self._due()
        alpha, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        self.app.dream_step(time.time())
        task = next(row for row in self.app.store.find("task", limit=20) if row.get("dreaming"))
        stage = Path(task["dream_stage"])
        (stage / "candidate" / "MEMORY.md").write_text("# Memory\nNO\n", encoding="utf-8")
        task["status"] = "cancelled"
        self.app.store.put("task", task)
        with self.assertRaises(dreaming.CandidateRejected):
            dreaming.accept_candidate(self.app, task)
        root = dreaming.memory_root(self.app, alpha["id"])
        self.assertFalse((root / "CURRENT").exists())

    def test_a_large_backlog_advances_only_the_batch_that_ran(self):
        self._due()
        project = self.app.create_project("bot-main", {"title": "Batch", "goal": "many"})
        for index in range(dreaming.SIGNAL_BATCH + 6):
            self.app.store.put("work", {
                "id": uid("work"), "owner_bot_id": "bot-main", "goal": "item", "status": "delivered",
                "project_id": project["id"], "created": time.time(),
                "checkpoint": {"phase": "build", "summary": "ITEM-%02d" % index},
            })
        self.app.dream_step(time.time())
        self._drain()
        first = dreaming.formal_watermark(self.app, project["id"]) or {}
        self.assertEqual(len(first), dreaming.SIGNAL_BATCH)
        row = self.app.store.get("settings", "dreaming")
        row["pending_slot"] = None
        row["attempted"] = []
        row["cooled"] = []
        row["armed_slot"] = time.time() - 1
        self.app.store.put("settings", row)
        self.app.dream_step(time.time())
        self._drain()
        second = dreaming.formal_watermark(self.app, project["id"]) or {}
        self.assertGreater(len(second), len(first))
        self.assertIn("ITEM-", dreaming.formal_index(self.app, project["id"]))

    def test_submit_backpressure_keeps_the_project_in_this_slot(self):
        self._due()
        self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        real = self.app.submit_task

        def blocked(*_args, **_kwargs):
            raise Fault("backpressure", 429)

        self.app.submit_task = blocked
        try:
            now = time.time()
            self.assertTrue(self.app.dream_step(now))
            works = [row for row in self.app.store.find("work", limit=20) if row.get("kind") == "dreaming"]
            self.assertEqual(len(works), 1)
            self.assertEqual(works[0]["status"], "active")
            self.assertFalse(self.app.dream_step(now + 1))
            self.assertEqual(len([row for row in self.app.store.find("work", limit=20) if row.get("kind") == "dreaming"]), 1)
        finally:
            self.app.submit_task = real
        self.app.dream_step(time.time() + dreaming.DEFER_S + 2)
        self.assertEqual(len([row for row in self.app.store.find("work", limit=20) if row.get("kind") == "dreaming"]), 1)
        self.assertTrue(any(row.get("dreaming") for row in self.app.store.find("task", limit=20)))

    def test_confinement_blocks_a_read_outside_the_stage(self):
        from pangpang import runner
        stage = Path(self.tmp.name) / "stage"
        (stage / "signal").mkdir(parents=True)
        (stage / "candidate").mkdir()
        (stage / "signal" / "in.txt").write_text("INSIDE", encoding="utf-8")
        outside = Path(self.tmp.name) / "other-project.txt"
        outside.write_text("SECRET-OTHER", encoding="utf-8")
        run = Path(self.tmp.name) / "run"
        run.mkdir()
        code = (
            "import pathlib,sys\n"
            "try:\n"
            "    print('OUT '+pathlib.Path(sys.argv[1]).read_text())\n"
            "except OSError as e:\n"
            "    print('OUT_DENIED')\n"
            "pathlib.Path('signal/in.txt').read_text()\n"
            "pathlib.Path('candidate/out.txt').write_text('ok')\n"
            "print('WRITE_OK')\n"
        )
        (run / "dream-confine.json").write_text(json.dumps({
            "readable": [str(stage)],
            "writable": [str(stage / "candidate")],
            "argv": [sys.executable, "-c", code, str(outside)],
        }), encoding="utf-8")
        import subprocess
        completed = subprocess.run(
            [sys.executable, str(Path(runner.__file__)), "confine", str(run)],
            cwd=stage, capture_output=True, text=True, timeout=20)
        self.assertIn("OUT_DENIED", completed.stdout)
        self.assertNotIn("SECRET-OTHER", completed.stdout)
        self.assertIn("WRITE_OK", completed.stdout)
        self.assertEqual((stage / "candidate" / "out.txt").read_text(encoding="utf-8"), "ok")

    def test_a_deep_leftover_is_resumed_past_the_shallow_sibling(self):
        project, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        stage = self.data.resolve().parent / "dream-stage" / project["id"] / "run-deep"
        signal = stage / "signal"
        signal.mkdir(parents=True)
        (signal / "note.txt").write_text("kept\n", encoding="utf-8")
        leaf = stage / "candidate"
        for _ in range(80):
            leaf = leaf / "n"
        leaf.mkdir(parents=True)
        (leaf / "leaf.txt").write_text("x", encoding="utf-8")
        for _ in range(4):
            self.app.dream_step(time.time())
        cursor = self.app.store.get("settings", "dreaming").get("cleanup_cursor") or {}
        frames = cursor.get("frames") or []
        self.assertTrue(frames)
        self.assertIn("candidate", frames[-1])
        for _ in range(90):
            self.app.dream_step(time.time())
            if not (leaf / "leaf.txt").exists():
                break
        self.assertFalse((leaf / "leaf.txt").exists())

    def test_unknown_cancel_does_not_publish(self):
        self._due()
        alpha, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        self.app.dream_step(time.time())
        task = next(row for row in self.app.store.find("task", limit=20) if row.get("dreaming"))
        stage = Path(task["dream_stage"])
        (stage / "candidate" / "MEMORY.md").write_text("# Memory\nNO\n", encoding="utf-8")
        task["status"] = "unknown"
        task["cancel_requested"] = True
        self.app.store.put("task", task)
        with self.assertRaises(dreaming.CandidateRejected):
            dreaming.accept_candidate(self.app, task)
        root = dreaming.memory_root(self.app, alpha["id"])
        self.assertFalse((root / "CURRENT").exists())

    def test_a_route_miss_does_not_build_another_work_while_waiting(self):
        self._due()
        self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        probe = self.app.store.get("probe", "codex-local")
        probe["available"] = False
        self.app.store.put("probe", probe)
        now = time.time()
        self.assertTrue(self.app.dream_step(now))
        self.assertEqual([row for row in self.app.store.find("work", limit=20) if row.get("kind") == "dreaming"], [])
        self.assertFalse(self.app.dream_step(now + 1))
        self.assertTrue(self.app.dream_step(now + dreaming.DEFER_S + 1))
        self.assertEqual([row for row in self.app.store.find("work", limit=20) if row.get("kind") == "dreaming"], [])
        probe["available"] = True
        self.app.store.put("probe", probe)
        self.app.dream_step(time.time() + 2 * dreaming.DEFER_S + 2)
        self.assertEqual(len([row for row in self.app.store.find("work", limit=20) if row.get("kind") == "dreaming"]), 1)
        self.assertTrue(any(row.get("dreaming") for row in self.app.store.find("task", limit=20)))

    def test_runtime_paths_are_the_program_not_a_data_parent(self):
        from pangpang import runner
        paths = runner._runtime_read_paths([sys.executable])
        self.assertIn(os.path.realpath(sys.executable), paths)
        self.assertTrue(any("python" in path for path in paths))
        self.assertFalse(any(path == "/opt" or path.startswith("/opt/") for path in paths))
        self.assertFalse(any(path == "/private" or path.startswith("/private/") for path in paths))
        self.assertNotIn("/usr", paths)
        self.assertNotIn("/Library", paths)

    def test_sync_recovery_uses_the_work_when_the_marker_is_missing(self):
        self._due()
        alpha, _work = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        self.app.dream_step(time.time())
        self._drain()
        root = dreaming.memory_root(self.app, alpha["id"])
        marker = root / "PENDING_SYNC"
        if marker.exists():
            marker.unlink()
        task = next(row for row in self.app.store.find("task", limit=20) if row.get("dreaming"))
        work = self.app.store.get("work", task["work_id"])
        work["dream_sync_pending"] = True
        work["status"] = "active"
        self.app.store.put("work", work)
        project = self.app.store.get("project", alpha["id"])
        project.pop("dreamed_at", None)
        self.app.store.put("project", project)
        dreaming.recover_sync(self.app)
        settled = self.app.store.get("work", work["id"])
        self.assertNotIn("dream_sync_pending", settled)
        self.assertEqual(settled["status"], "delivered")
        self.assertTrue(self.app.store.get("project", alpha["id"]).get("dreamed_at"))

    def test_watermark_chunks_cover_many_batches_without_a_chain(self):
        project = self.app.create_project("bot-main", {"title": "Marks", "goal": "many"})
        root = dreaming.memory_root(self.app, project["id"])
        root.mkdir(parents=True)
        for index in range(120):
            dreaming._append_mark(root, {"item-%d" % index: "token-%d" % index})
        found = dreaming._read_watermark(root)
        self.assertEqual(len(found), 120)
        self.assertEqual(found["item-0"], "token-0")
        self.assertEqual(found["item-119"], "token-119")
        order = json.loads((root / "mark" / "order.json").read_text(encoding="utf-8"))
        self.assertLessEqual(len(order), 8)

    def test_cancel_uses_the_dream_work_before_a_task_exists(self):
        project = self.app.create_project("bot-main", {"title": "Cancel", "goal": "hold"})
        work = self.app.store.put("work", {
            "id": uid("work"), "owner_bot_id": "bot-main", "goal": "dream", "status": "active",
            "kind": "dreaming", "project_id": project["id"], "created": time.time(),
        })
        ids = self.app._dream_projects_for_cancel(work["id"])
        self.assertIn(project["id"], ids)
        self.assertEqual([row for row in self.app.store.find("task", limit=20) if row.get("work_id") == work["id"]], [])

    def test_a_waiting_stage_is_not_deleted_before_its_task_exists(self):
        project = self.app.create_project("bot-main", {"title": "Wait", "goal": "hold"})
        stage = self.data.resolve().parent / "dream-stage" / project["id"] / "run-waiting"
        (stage / "signal").mkdir(parents=True)
        (stage / "signal" / "note.txt").write_text("stay\n", encoding="utf-8")
        (stage / "candidate").mkdir()
        row = dreaming.load(self.app)
        row["held"] = {project["id"]: {"work_id": "work-waiting", "stage": str(stage.resolve()), "dream": {}}}
        dreaming._save(self.app, row)
        for _ in range(3):
            dreaming.sweep_finished_dirs(self.app)
        self.assertEqual((stage / "signal" / "note.txt").read_text(encoding="utf-8"), "stay\n")

    def test_file_rules_are_not_dropped(self):
        from pangpang import runner
        target = Path(self.tmp.name) / "libpython.so"
        target.write_bytes(b"\x7fELF")
        folder = Path(self.tmp.name) / "stdlib"
        folder.mkdir()
        profile = runner._sandbox_profile([str(target), str(folder)], [])
        self.assertIn('(literal "%s")' % target, profile)
        self.assertIn('(subpath "%s")' % folder, profile)
        missing = str(Path(self.tmp.name) / "missing")
        self.assertEqual(runner._present_paths([str(target), str(folder), missing]),
                         [str(target), str(folder)])

    def test_cleanup_skip_does_not_stall_past_the_first_page(self):
        project = self.app.create_project("bot-main", {"title": "Pages", "goal": "hold"})
        base = self.data.resolve().parent / "dream-stage" / project["id"]
        held = {}
        for index in range(64):
            stage = base / ("held-%03d" % index)
            stage.mkdir(parents=True)
            held["h%03d" % index] = {"work_id": "w", "stage": str(stage.resolve()), "dream": {}}
        for index in range(10):
            (base / ("run-%03d" % index)).mkdir(parents=True)
        row = dreaming.load(self.app)
        row["held"] = held
        dreaming._save(self.app, row)
        dreaming._WALK.reads = 0
        total = 0
        for _ in range(8):
            before = dreaming._WALK.reads
            dreaming.sweep_finished_dirs(self.app)
            self.assertLessEqual(dreaming._WALK.reads - before, dreaming.CLEANUP_BUDGET)
            total += dreaming._WALK.reads - before
            if not [path for path in base.iterdir() if path.name.startswith("run-")]:
                break
        self.assertEqual([path.name for path in base.iterdir() if path.name.startswith("run-")], [])
        self.assertEqual(len([path for path in base.iterdir() if path.name.startswith("held-")]), 64)
        self.assertLess(total, 250)

    def test_a_watermark_larger_than_the_commit_cap_stays_complete(self):
        project = self.app.create_project("bot-main", {"title": "Wide", "goal": "hold"})
        root = dreaming.memory_root(self.app, project["id"])
        chunk = root / "mark"
        chunk.mkdir(parents=True)
        payload = {"k%05d" % index: "v" * 80 for index in range(20000)}
        text = json.dumps(payload, sort_keys=True)
        self.assertGreater(len(text.encode("utf-8")), dreaming.COMMIT_MAX)
        (chunk / "wide.json").write_text(text, encoding="utf-8")
        (chunk / "order.json").write_text(json.dumps(["wide.json"]), encoding="utf-8")
        found = dreaming._read_watermark(root)
        self.assertEqual(len(found), 20000)
        self.assertEqual(found["k00000"], "v" * 80)
        self.assertEqual(found["k19999"], "v" * 80)

    def test_a_published_batch_is_appended_on_recovery(self):
        project = self.app.create_project("bot-main", {"title": "Recover mark", "goal": "hold"})
        root = dreaming.memory_root(self.app, project["id"])
        gen = "gen-" + "cd" * 16
        folder = root / "g" / gen
        folder.mkdir(parents=True)
        (folder / "READY").write_text("ok\n", encoding="utf-8")
        (folder / "commit.json").write_text(json.dumps({
            "task_id": "task-1", "attempt_uuid": None, "base_gen": "",
            "batch": {"work:alpha": "token-alpha"},
        }), encoding="utf-8")
        (root / "CURRENT").write_text(gen + "\n", encoding="utf-8")
        real = dreaming._append_mark

        def fail_once(root, batch):
            dreaming._append_mark = real
            raise OSError("mark disk")

        dreaming._append_mark = fail_once
        try:
            dreaming.recover_marks(self.app)
        finally:
            dreaming._append_mark = real
        self.assertFalse((root / "mark" / "order.json").exists())
        dreaming.recover_marks(self.app)
        order = json.loads((root / "mark" / "order.json").read_text(encoding="utf-8"))
        stored = (root / "mark" / order[0]).read_text(encoding="utf-8")
        self.assertIn("work:alpha", stored)
        self.assertEqual(dreaming._chunk_watermark(root).get("work:alpha"), "token-alpha")
        before = (root / "mark" / "order.json").read_text(encoding="utf-8")
        dreaming.recover_marks(self.app)
        self.assertEqual((root / "mark" / "order.json").read_text(encoding="utf-8"), before)

    def _publish_chain(self, project, batches):
        root = dreaming.memory_root(self.app, project["id"])
        base = ""
        gen = ""
        for index, batch in enumerate(batches):
            gen = "gen-" + ("%02x" % (index + 1)) * 16
            folder = root / "g" / gen
            folder.mkdir(parents=True)
            (folder / "READY").write_text("ok\n", encoding="utf-8")
            (folder / "commit.json").write_text(json.dumps({
                "task_id": "task-%d" % index, "attempt_uuid": None, "base_gen": base, "batch": batch,
            }), encoding="utf-8")
            base = gen
        (root / "CURRENT").write_text(gen + "\n", encoding="utf-8")
        return root, gen

    def test_every_unpublished_batch_is_recovered_and_blocks_the_next_publish(self):
        project, work_id = self._project("Alpha", "ALPHA-SUMMARY", "alpha")
        work = self.app.store.get("work", work_id)
        older = dreaming._work_token(work)
        newer = dreaming._work_token(dict(work, checkpoint=dict(work["checkpoint"], summary="later")))
        root, gen = self._publish_chain(project, [
            {"work:" + work_id: older},
            {"work:later": newer},
        ])
        (root / "mark").mkdir(parents=True)
        (root / "mark" / "only-new.json").write_text(json.dumps({"work:later": newer}), encoding="utf-8")
        (root / "mark" / "order.json").write_text(json.dumps(["only-new.json"]), encoding="utf-8")
        self.assertTrue(dreaming._has_new(dreaming.formal_watermark(self.app, project["id"]), [work], [], []))
        real = dreaming._append_mark

        def fail_append(root, batch):
            raise OSError("mark disk")

        dreaming._append_mark = fail_append
        try:
            dreaming.recover_marks(self.app)
            self.assertTrue(dreaming._mark_gap(root))
            stage = self.data.resolve().parent / "dream-stage" / project["id"] / "run-next"
            stage.mkdir(parents=True)
            task = self.app.store.put("task", {
                "id": uid("task"), "attempt_uuid": "attempt-1", "status": "running",
                "dreaming": True, "dreaming_project_id": project["id"], "work_id": work_id,
                "dream_stage": str(stage), "dream_base_gen": gen, "dream_cursor": {},
            })
            with self.assertRaises(dreaming.CandidateRejected):
                dreaming.accept_candidate(self.app, task)
            self.assertEqual((root / "CURRENT").read_text(encoding="utf-8").strip(), gen)
        finally:
            dreaming._append_mark = real
        dreaming.recover_marks(self.app)
        self.assertFalse(dreaming._mark_gap(root))
        remembered = dreaming.formal_watermark(self.app, project["id"])
        self.assertEqual(remembered.get("work:" + work_id), older)
        self.assertEqual(remembered.get("work:later"), newer)
        self.assertFalse(dreaming._has_new(remembered, [work], [], []))

    def test_watermark_is_not_written_while_directory_sync_is_failing(self):
        project = self.app.create_project("bot-main", {"title": "Sync", "goal": "hold"})
        root, _gen = self._publish_chain(project, [{"work:alpha": "token-alpha"}])
        (root / "PENDING_SYNC").write_text("pending\n", encoding="utf-8")
        dreaming.recover_marks(self.app)
        self.assertFalse((root / "mark").exists())
        (root / "PENDING_SYNC").unlink()
        real = dreaming._fsync_ancestors

        def fail_sync(path):
            raise OSError("sync")

        dreaming._fsync_ancestors = fail_sync
        try:
            dreaming.recover_marks(self.app)
            self.assertFalse((root / "mark").exists())
        finally:
            dreaming._fsync_ancestors = real
        dreaming.recover_marks(self.app)
        self.assertEqual(dreaming._chunk_watermark(root).get("work:alpha"), "token-alpha")

    def test_a_home_directory_is_not_a_runtime_read(self):
        from pangpang import runner
        self.assertNotIn("/home/user", runner._runtime_read_paths(["/home/user/python"]))
        self.assertNotIn("/home/user", runner._runtime_read_paths(["/home/user/bin/tool"]))
        self.assertFalse(runner.runtime_grant_possible(["/home/user/python"]))
        self.assertFalse(runner.runtime_grant_possible(["/Users/user/claude"]))
        self.assertTrue(runner.runtime_grant_possible([sys.executable]))
        argv = self.app.execution.installations["codex-local"]["argv"]
        self.app.execution.installations["codex-local"]["argv"] = ["/home/user/python"]
        try:
            with self.assertRaises(Fault):
                self.app.set_dreaming({
                    "enabled": True, "installation_id": "codex-local", "time": "02:00",
                })
            offered = [item for item in self.app.dreaming_view()["executors"] if item["id"] == "codex-local"]
            self.assertFalse(offered[0]["bounded"])
        finally:
            self.app.execution.installations["codex-local"]["argv"] = argv

    def test_cancelling_a_held_work_drops_the_retry(self):
        project = self.app.create_project("bot-main", {"title": "Held", "goal": "hold"})
        work = self.app.store.put("work", {
            "id": uid("work"), "owner_bot_id": "bot-main", "goal": "dream", "status": "active",
            "kind": "dreaming", "project_id": project["id"], "created": time.time(),
        })
        self._enable()
        row = dreaming.load(self.app)
        row["held"] = {project["id"]: {"work_id": work["id"], "stage": "/tmp/dream-held", "dream": {}}}
        row["pending_slot"] = time.time() - 1
        row["retry_not_before"] = time.time() - 1
        dreaming._save(self.app, row)
        self.app.cancel_work(work["id"])
        self.assertNotIn(project["id"], dreaming.load(self.app).get("held") or {})
        self.app.dream_step(time.time())
        self.assertEqual(
            [task for task in self.app.store.find("task", limit=20) if task.get("work_id") == work["id"]],
            [])
        self.assertNotIn(project["id"], dreaming.load(self.app).get("held") or {})
