""" Gemini CLI tier 2: interrupt the running turn, resume the same session.

Gemini CLI 0.62.0 has no online stdin inject (no input-format). Headless reads
the prompt from a non-TTY stdin. `--session-id` is the derived attempt id;
SIGINT stops the turn; `--resume` of that same id continues it. `--session-id`
and `--resume` together are rejected by the CLI. This is not tier 1 inject.
"""
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang import runner
from pangpang.execution import ADAPTER_CATALOG, CLI_SESSION_RESUME, interjection_tier_of
from pangpang.runtime import TOOLS, Runtime
from pangpang.service import INJECT_UNSUPPORTED_ADVICE, Service
from pangpang.store import Fault


def _tool(name, arguments, call_id="call-gemini"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


class CapabilityTests(unittest.TestCase):
    def test_gemini_is_tier_2_without_an_stdin_protocol(self):
        row = ADAPTER_CATALOG["gemini"]
        self.assertEqual(interjection_tier_of(row), 2)
        self.assertEqual(row["resume_protocol"], CLI_SESSION_RESUME)
        self.assertNotIn("stdin_protocol", row)
        self.assertEqual(row["argv"], ["--skip-trust"])
        self.assertEqual(row["priority"], 35)
        self.assertLess(ADAPTER_CATALOG["codex"]["priority"], row["priority"])
        self.assertLess(row["priority"], ADAPTER_CATALOG["pi"]["priority"])
        joined = " ".join(row["argv"])
        for flag in ("--yolo", "-y", "--approval-mode", "--acp", "--experimental-acp",
                     "--fork-session", "--session-id", "--resume", "priority"):
            self.assertNotIn(flag, joined)
        # A tier number without a protocol this runner implements stays tier 3.
        self.assertEqual(interjection_tier_of(
            {"interjection_tier": 2, "stdin_protocol": "gemini-stream"}), 3)
        self.assertEqual(interjection_tier_of({"interjection_tier": 2}), 3)
        self.assertEqual(interjection_tier_of(
            {"interjection_tier": 2, "resume_protocol": "not-a-protocol"}), 3)
        # Same declaration, any adapter name. Not a gemini special case.
        self.assertEqual(interjection_tier_of(
            {"interjection_tier": 2, "resume_protocol": CLI_SESSION_RESUME}), 2)
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG["claude-code"]), 1)
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG["codex"]), 1)
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG.get("legacy-executor")), 3)
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG["pi"]), 3)

    def test_session_argv_is_one_derived_id_and_never_both_flags(self):
        execution_id = "exec-" + "ab" * 16
        session = runner.session_id_from_execution(execution_id)
        self.assertEqual(session, str(uuid.UUID(hex="ab" * 16)))
        spec = {
            "argv": ["gemini", "--skip-trust"],
            "execution_id": execution_id,
            "resume_protocol": CLI_SESSION_RESUME,
        }
        first = runner.executor_argv(spec, "SENTINEL_PROMPT_DO_NOT_LEAK")
        self.assertIn("--session-id=" + session, first)
        self.assertNotIn("--resume=" + session, first)
        self.assertFalse(any(part == "--resume" or part.startswith("--resume=") for part in first))
        self.assertNotIn("SENTINEL_PROMPT_DO_NOT_LEAK", first)
        self.assertIn("--skip-trust", first)
        again = runner.executor_argv(spec, "SENTINEL_PROMPT_DO_NOT_LEAK", resume=True)
        self.assertIn("--resume=" + session, again)
        self.assertFalse(any(part == "--session-id" or part.startswith("--session-id=") for part in again))
        self.assertNotIn("SENTINEL_PROMPT_DO_NOT_LEAK", again)
        for argv in (first, again):
            self.assertNotIn("--fork-session", argv)
            self.assertNotIn("--yolo", argv)
            self.assertNotIn("--approval-mode", argv)
            self.assertNotIn("--acp", argv)
            self.assertTrue(all("priority" not in part for part in argv))
            self.assertEqual(
                sum(part.startswith("--session-id=") or part.startswith("--resume=") for part in argv),
                1)
        with self.assertRaises(ValueError):
            runner.executor_argv(
                {"argv": ["gemini", "--fork-session"], "execution_id": execution_id,
                 "resume_protocol": CLI_SESSION_RESUME}, "x")
        # A stored session flag is replaced, not stacked, and latest is never passed.
        replaced = runner.executor_argv(
            {"argv": ["gemini", "--resume", "latest", "--session-id", session],
             "execution_id": execution_id, "resume_protocol": CLI_SESSION_RESUME},
            "x", resume=True)
        self.assertEqual(replaced, ["gemini", "--resume=" + session])
        self.assertNotIn("latest", replaced)


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.app = Service(config(self.workspace), self.root / "data")
        self.app.probe("codex-local")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _running_task(self, key):
        sent = self.app.send("bot-main", {"text": "执行", "allow_write": True}, key)
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "写一节"})
        task = self.app.store.get("task", submitted["task_id"])
        task["status"] = "running"
        self.app.store.put("task", task)
        turn = self.app.store.get("turn", sent["turn_id"])
        turn["status"] = "running"
        self.app.store.put("turn", turn)
        dep = self.app.store.get("dependency", task["dependency_id"])
        directory = Path(self.app.execution.directory(
            self.app.execution.hosts[task["host_id"]], task["execution_id"]))
        directory.mkdir(parents=True, exist_ok=True)
        return sent, task, dep, turn, directory

    def test_tier_2_reuses_send_bot_message_and_the_mailbox(self):
        sent, task, dep, turn, directory = self._running_task("gemini")
        self.app.execution.installations["codex-local"]["adapter"] = "gemini"
        described = {item["function"]["name"]: item["function"] for item in TOOLS}
        self.assertNotIn("priority", described["send_bot_message"]["parameters"]["properties"])
        spec = self.app.execution.spec(self.app.store.get("task", task["id"]))
        self.assertEqual(spec["resume_protocol"], CLI_SESSION_RESUME)
        self.assertNotIn("stdin_protocol", spec)
        result = self.app.send_bot_message("bot-main", sent["work_id"], {
            "dep_id": dep["id"], "text": " 加一句 ", "refs": ["artifact-nope"],
            "target_work_id": task["id"], "requires_response": True, "priority": "now"})
        self.assertEqual(result["tier"], 2)
        self.assertTrue(result["queued"])
        self.assertNotIn("interrupted", result)
        self.assertNotIn("injected", result)
        self.assertFalse(result["yielded"])
        rows = [json.loads(line) for line in (directory / "injects.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["op"] for row in rows], ["user", "interrupt"])
        self.assertEqual(rows[0]["text"], "加一句")
        self.assertEqual(rows[0]["uuid"], result["uuid"])
        self.assertEqual(rows[1]["id"], result["uuid"])
        self.assertTrue(all("priority" not in row for row in rows))
        self.assertNotIn("refs", json.dumps(rows))
        # The same call id journals one pair. A second sentence is a new row.
        runtime = Runtime(self.app, model=object())
        body = {"dep_id": dep["id"], "text": "再补一句"}
        first = runtime.durable_tool(turn, _tool("send_bot_message", body))
        again = runtime.durable_tool(turn, _tool("send_bot_message", body))
        self.assertEqual(first["uuid"], again["uuid"])
        self.assertEqual(first["tier"], 2)
        rows = [json.loads(line) for line in (directory / "injects.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["text"] for row in rows if row["op"] == "user"], ["加一句", "再补一句"])
        self.assertEqual(sum(row["op"] == "interrupt" for row in rows), 2)

    def test_same_protocol_on_another_adapter_name_is_tier_2(self):
        sent, _, dep, _, directory = self._running_task("other")
        ADAPTER_CATALOG["other-cli"] = {
            "binary": "other-cli", "argv": ["--skip-trust"], "concurrency": 1, "priority": 35,
            "interjection_tier": 2, "resume_protocol": CLI_SESSION_RESUME}
        self.addCleanup(ADAPTER_CATALOG.pop, "other-cli", None)
        self.app.execution.installations["codex-local"]["adapter"] = "other-cli"
        result = self.app.send_bot_message("bot-main", sent["work_id"], {
            "dep_id": dep["id"], "text": "加一句"})
        self.assertEqual(result["tier"], 2)
        self.assertTrue((directory / "injects.jsonl").exists())

    def test_undeclared_executor_stays_tier_3(self):
        sent, _, dep, _, directory = self._running_task("qwen")
        self.app.execution.installations["codex-local"]["adapter"] = "legacy-executor"
        with self.assertRaises(Fault) as caught:
            self.app.send_bot_message("bot-main", sent["work_id"], {
                "dep_id": dep["id"], "text": "加一句"})
        self.assertEqual(caught.exception.code, "interjection_unsupported")
        self.assertEqual(caught.exception.advice, INJECT_UNSUPPORTED_ADVICE)
        self.assertFalse((directory / "injects.jsonl").exists())


GEMINI_STUB = r"""
import pathlib, signal, sys, json
turn_path = pathlib.Path("gemini-turn.txt")
n = int(turn_path.read_text()) + 1 if turn_path.exists() else 1
turn_path.write_text(str(n))
pathlib.Path("gemini-argv-%d.json" % n).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
pathlib.Path("gemini-stdin-%d.txt" % n).write_text(sys.stdin.read(), encoding="utf-8")
if n == 1:
    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGINT, stop)
    pathlib.Path("gemini-ready-1").write_text("ready", encoding="utf-8")
    while True:
        signal.pause() if hasattr(signal, "pause") else __import__("time").sleep(0.05)
pathlib.Path("gemini-ready-2").write_text("ready", encoding="utf-8")
sys.stdout.write("FINAL_ANSWER\n")
sys.stdout.flush()
"""


@unittest.skipUnless(os.name == "posix", "process-group supervise is POSIX")
class ResumeSuperviseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.stub = self.root / "stub_gemini.py"
        self.stub.write_text(GEMINI_STUB, encoding="utf-8")
        self.app = Service(config(self.workspace), self.root / "data")
        self.app.store.put("probe", {
            "id": "codex-local", "available": True,
            "supervision": runner.PGROUP_COOPERATIVE})
        self.thread = None
        self.directory = None

    def tearDown(self):
        if self.thread is not None and self.thread.is_alive() and self.directory is not None:
            started = runner.read_json_bounded(self.directory / "started.json") or {}
            pid = started.get("pid")
            if isinstance(pid, int) and pid > 0:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
            self.thread.join(5)
        self.app.store.close()
        self.tmp.cleanup()

    def _start(self, prompt):
        install = self.app.execution.installations["codex-local"]
        install["adapter"] = "gemini"
        install["argv"] = [sys.executable, str(self.stub), "--skip-trust"]
        install["prompt_mode"] = "stdin"
        sent = self.app.send("bot-main", {"text": "task", "allow_write": True}, prompt[:12])
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": prompt})
        task = self.app.store.get("task", submitted["task_id"])
        task["attempt_uuid"] = "attempt-gemini"
        self.app.store.put("task", task)
        task = self.app.store.get("task", task["id"])
        host = self.app.execution.hosts[task["host_id"]]
        directory = Path(self.app.execution.directory(host, task["execution_id"]))
        spec = self.app.execution.spec(task)
        self.assertEqual(spec["resume_protocol"], CLI_SESSION_RESUME)
        self.assertNotIn("stdin_protocol", spec)
        self.app.execution.prepare(host, str(directory), spec)
        self.directory = directory

        def work():
            with patch.object(runner, "_detect_supervision", return_value=runner.PGROUP_COOPERATIVE):
                runner.supervise(directory)

        self.thread = threading.Thread(target=work)
        self.thread.start()
        return task

    def test_interrupt_resumes_the_same_derived_session(self):
        task = self._start("SENTINEL_PROMPT_DO_NOT_LEAK")
        ready = self.workspace / "gemini-ready-1"
        deadline = time.time() + 8
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists(), "first turn did not start")
        message = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        runner.accept_inject(self.directory, {
            "uuid": message, "text": "加一句", "interrupt": message})
        self.thread.join(15)
        self.assertFalse(self.thread.is_alive())
        session = runner.session_id_from_execution(task["execution_id"])
        first = json.loads((self.workspace / "gemini-argv-1.json").read_text(encoding="utf-8"))
        second = json.loads((self.workspace / "gemini-argv-2.json").read_text(encoding="utf-8"))
        self.assertIn("--session-id=" + session, first)
        self.assertNotIn("--resume=" + session, first)
        self.assertIn("--resume=" + session, second)
        self.assertFalse(any(part.startswith("--session-id") for part in second))
        self.assertNotIn("--fork-session", first + second)
        self.assertNotIn("--yolo", first + second)
        self.assertNotIn("--acp", first + second)
        self.assertNotIn("SENTINEL_PROMPT_DO_NOT_LEAK", first + second)
        self.assertIn("SENTINEL_PROMPT_DO_NOT_LEAK",
                      (self.workspace / "gemini-stdin-1.txt").read_text(encoding="utf-8"))
        self.assertEqual((self.workspace / "gemini-stdin-2.txt").read_text(encoding="utf-8"), "加一句")
        self.assertTrue((self.directory / "session-resume").is_file())
        receipt = runner.inspect(self.directory, include_tail=True)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["interjection_tier"], 2)
        self.assertEqual(receipt["session_id"], session)
        self.assertEqual(receipt["resumed"], [{"uuid": message, "resumed": True}])
        self.assertNotIn("injects", receipt)
        self.assertIn("FINAL_ANSWER", receipt["tail"])

    def test_cancel_does_not_resume(self):
        self._start("不要续")
        ready = self.workspace / "gemini-ready-1"
        deadline = time.time() + 8
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists())
        # Cancel is visible before the sentence, so the grace path wins and
        # the waiting sentence does not start a second identity.
        runner.request_cancel(self.directory)
        message = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        runner.accept_inject(self.directory, {
            "uuid": message, "text": "加一句", "interrupt": message})
        self.thread.join(15)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse((self.workspace / "gemini-argv-2.json").exists())
        self.assertFalse((self.directory / "session-resume").exists())
        receipt = runner.inspect(self.directory)
        self.assertEqual(receipt["status"], "cancelled")
        self.assertEqual(receipt["interjection_tier"], 2)
        self.assertEqual(receipt["resumed"], [{
            "uuid": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", "resumed": False}])


if __name__ == "__main__":
    unittest.main()
