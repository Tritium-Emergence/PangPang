""" Claude Code tier 1: one user sentence into the running task.

Online inject reuses send_bot_message. The runner speaks stream-json, keeps
stdin open, and records delivery on the receipt. Undeclared executors stay on
tier 3. Gemini tier 2 is not implemented here. Codex declares its own protocol.
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
from pangpang.execution import ADAPTER_CATALOG, interjection_tier_of
from pangpang.runtime import TOOLS, Runtime
from pangpang.service import INJECT_UNSUPPORTED_ADVICE, Service
from pangpang.store import Fault


def _tool(name, arguments, call_id="call-inject"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


class CapabilityTests(unittest.TestCase):
    def test_only_a_declared_protocol_is_tier_1(self):
        self.assertEqual(interjection_tier_of(None), 3)
        self.assertEqual(interjection_tier_of({}), 3)
        self.assertEqual(interjection_tier_of({"interjection_tier": 1}), 3)
        self.assertEqual(interjection_tier_of(
            {"interjection_tier": 2, "stdin_protocol": "gemini-stream"}), 3)
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG["codex"]), 1)
        self.assertEqual(ADAPTER_CATALOG["codex"]["stdin_protocol"], "codex-app-server")
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"],
                         ["exec", "--sandbox", "workspace-write", "-"])
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG.get("qwen-claude")), 3)
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG["pi"]), 3)
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG["claude-code"]), 1)
        self.assertEqual(ADAPTER_CATALOG["claude-code"]["stdin_protocol"], "claude-stream-json")

    def test_stream_argv_keeps_the_prompt_off_the_process_list(self):
        execution_id = "exec-" + "ab" * 16
        sentinel = "SENTINEL_PROMPT_DO_NOT_LEAK"
        spec = {
            "argv": ["-p", "--permission-mode", "acceptEdits", "--allowed-tools", "Read", "Write"],
            "execution_id": execution_id,
            "stdin_protocol": "claude-stream-json",
        }
        argv = runner.executor_argv(spec, sentinel)
        self.assertIn("--input-format=stream-json", argv)
        self.assertIn("--output-format=stream-json", argv)
        self.assertIn("--verbose", argv)
        self.assertIn("--replay-user-messages", argv)
        session = runner.session_id_from_execution(execution_id)
        self.assertIn("--session-id=" + session, argv)
        self.assertEqual(session, str(uuid.UUID(hex="ab" * 16)))
        self.assertNotIn("--fork-session", argv)
        self.assertNotIn(sentinel, argv)
        self.assertEqual(argv[:6], spec["argv"])
        # Valued flags are one token. --verbose is the required boolean.
        self.assertTrue(all(
            "=" in part for part in argv[6:]
            if part not in ("--replay-user-messages", "--verbose")))
        self.assertIn("--permission-mode", argv)
        self.assertIn("acceptEdits", argv)
        plain = runner.executor_argv({"argv": ["bin", "{prompt}"]}, sentinel)
        self.assertEqual(plain, ["bin", sentinel])


class StreamResultTests(unittest.TestCase):
    def test_final_result_event_is_the_text_not_the_chatter(self):
        raw = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": "partial chatter"}}),
            json.dumps({"type": "result", "result": "first", "user_message_uuids": ["old"]}),
            "not json",
            json.dumps({"type": "control_request", "request": {
                "subtype": "can_use_tool", "tool_name": "Bash"}}),
            json.dumps({"type": "result", "result": "FINAL_ANSWER", "user_message_uuids": ["u1"]}),
        ])
        text, confirmed = runner.summarize_stream(raw)
        self.assertEqual(text, "FINAL_ANSWER")
        self.assertEqual(confirmed, {"old", "u1"})
        # No result event: do not treat the log itself as the result.
        chatter = json.dumps({"type": "assistant", "message": {"content": "only chatter"}})
        self.assertIsNone(runner.summarize_stream(chatter)[0])
        self.assertEqual(runner.summarize_stream(chatter)[1], set())

    def test_older_cli_single_uuid_confirms_delivery(self):
        #: Claude Code 2.1.251 result frames carry only user_message_uuid
        # (probed against the real CLI); the list arrived later. Without the
        # fallback a landed correction was recorded as delivered: false.
        raw = "\n".join([
            json.dumps({"type": "user", "uuid": "u1", "isReplay": True}),
            json.dumps({"type": "result", "result": "first", "user_message_uuid": "u1"}),
            json.dumps({"type": "user", "uuid": "u2", "isReplay": True}),
            json.dumps({"type": "result", "result": "FINAL", "user_message_uuid": "u2"}),
        ])
        self.assertEqual(runner.summarize_stream(raw), ("FINAL", {"u1", "u2"}))
        # The list wins when present; a replay echo alone is not delivery.
        both = json.dumps({"type": "result", "result": "x", "user_message_uuid": "last",
                           "user_message_uuids": ["first", "last"]})
        self.assertEqual(runner.summarize_stream(both)[1], {"first", "last"})
        echo_only = json.dumps({"type": "user", "uuid": "u3", "isReplay": True})
        self.assertEqual(runner.summarize_stream(echo_only)[1], set())

    def test_inspect_tail_is_the_result_event(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        raw = json.dumps({"type": "assistant", "message": {"content": "partial chatter"}}) + "\n"
        (directory / "output.log").write_text(raw, encoding="utf-8")
        runner.write_json(directory / "receipt.json", {
            "status": "succeeded", "execution_id": "exec-" + "cd" * 16, "spec_digest": "digest",
            "result_text": "FINAL_ANSWER", "interjection_tier": 1,
            "injects": [{"uuid": "u1", "delivered": True}],
        })
        seen = runner.inspect(directory, include_tail=True)
        self.assertEqual(seen["tail"], "FINAL_ANSWER")
        self.assertNotIn("partial chatter", seen["tail"])
        # Without an extracted result, the log tail is still the observation.
        bare = Path(tmp.name) / "bare"
        bare.mkdir()
        (bare / "output.log").write_text("plain executor output", encoding="utf-8")
        runner.write_json(bare / "receipt.json", {
            "status": "succeeded", "execution_id": "exec-" + "ef" * 16, "spec_digest": "digest",
        })
        self.assertEqual(runner.inspect(bare, include_tail=True)["tail"], "plain executor output")


class SteerChannelTests(unittest.TestCase):
    def test_inject_frame_has_no_priority_and_interrupt_cancels_the_queue(self):
        session = runner.session_id_from_execution("exec-" + "11" * 16)
        frame = runner.user_message_frame("加一句", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", session)
        self.assertNotIn("priority", frame)
        self.assertEqual(set(frame), {"type", "message", "uuid", "session_id"})
        self.assertEqual(frame["message"], {"role": "user", "content": "加一句"})
        interrupt = runner.interrupt_frame("req-1")
        self.assertEqual(interrupt["request"]["subtype"], "interrupt")
        self.assertIs(interrupt["request"]["cancel_queued"], True)
        self.assertNotIn("priority", interrupt)
        with self.assertRaises(ValueError):
            runner._write_frame(object(), {"type": "user", "priority": "now", "message": {}})

    def test_interrupt_resends_only_unconfirmed_injects(self):
        first_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        second_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        rows = [
            {"op": "user", "uuid": first_id, "text": "one"},
            {"op": "user", "uuid": second_id, "text": "two"},
        ]
        written, seen = set(), set()
        self.assertEqual([item[0] for item in runner.steer_actions(rows, set(), written, seen)],
                         ["user", "user"])
        rows.append({"op": "interrupt", "id": "int-1"})
        second = runner.steer_actions(rows, {first_id}, written, seen)
        self.assertEqual(second[0][0], "interrupt")
        resent = [item[1]["uuid"] for item in second if item[0] == "user"]
        self.assertEqual(resent, [second_id])
        session = runner.session_id_from_execution("exec-" + "22" * 16)
        for kind, row in second:
            if kind != "user":
                continue
            frame = runner.user_message_frame(row["text"], row["uuid"], session)
            self.assertNotIn("priority", frame)
            self.assertNotEqual(frame.get("priority"), "now")

    def test_stdin_stays_open_until_a_result_covers_the_last_write(self):
        # A result that already happened does not cover a frame written after it.
        due, idle = runner.stream_idle_due(1, 1, False, None, 10.0)
        self.assertFalse(due)
        self.assertIsNone(idle)
        # The opening prompt is written at seq 0. The first result may close.
        due, idle = runner.stream_idle_due(1, 0, False, None, 10.0)
        self.assertFalse(due)
        self.assertEqual(idle, 10.0)
        due, idle = runner.stream_idle_due(
            1, 0, False, idle, 10.0 + runner.STREAM_IDLE_CLOSE_S)
        self.assertTrue(due)
        # A mailbox row still to send keeps the pipe open.
        due, idle = runner.stream_idle_due(2, 0, True, 10.0, 99.0)
        self.assertFalse(due)
        self.assertIsNone(idle)

    def test_mailbox_rejects_priority_now(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        message = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        with self.assertRaises(ValueError):
            runner.accept_inject(directory, {"uuid": message, "text": "加一句", "priority": "now"})
        self.assertFalse((directory / "injects.jsonl").exists())
        accepted = runner.append_inject(directory, message, "加一句")
        self.assertEqual(accepted["uuid"], message)
        line = json.loads((directory / "injects.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(set(line), {"op", "uuid", "text"})
        self.assertNotIn("priority", line)
        self.assertEqual(line["text"], "加一句")
        # Same uuid is one row, not a second delivery.
        runner.append_inject(directory, message, "加一句")
        self.assertEqual((directory / "injects.jsonl").read_text(encoding="utf-8").count("\n"), 1)


class InjectOwnershipTests(unittest.TestCase):
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

    def test_other_works_dependency_is_rejected(self):
        _, _, dep, _, directory = self._running_task("owner")
        other = self.app.send("bot-main", {"text": "另一件", "allow_write": True}, "other")
        turn = self.app.store.get("turn", other["turn_id"])
        turn["status"] = "running"
        self.app.store.put("turn", turn)
        with self.assertRaises(Fault) as caught:
            self.app.send_bot_message("bot-main", other["work_id"], {
                "dep_id": dep["id"], "text": "加一句"})
        self.assertEqual(caught.exception.code, "dependency_forbidden")
        work = self.app.store.get("work", other["work_id"])
        self.assertEqual(caught.exception.advice, self.app.dependency_forbidden_advice(work))
        self.assertFalse((directory / "injects.jsonl").exists())

    def test_undeclared_executor_advises_tier_3(self):
        sent, _, dep, _, directory = self._running_task("codex")
        self.app.execution.installations["codex-local"]["adapter"] = "qwen-claude"
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG.get("qwen-claude")), 3)
        with self.assertRaises(Fault) as caught:
            self.app.send_bot_message("bot-main", sent["work_id"], {
                "dep_id": dep["id"], "text": "加一句"})
        self.assertEqual(caught.exception.code, "interjection_unsupported")
        self.assertEqual(caught.exception.advice, INJECT_UNSUPPORTED_ADVICE)
        self.assertIn("cancel_dependency", caught.exception.advice)
        self.assertIn("output_dir", caught.exception.advice)
        self.assertIn("submit_task", caught.exception.advice)
        self.assertFalse((directory / "injects.jsonl").exists())

    def test_claude_inject_is_user_text_only(self):
        sent, task, dep, turn, directory = self._running_task("claude")
        self.app.execution.installations["codex-local"]["adapter"] = "claude-code"
        described = {item["function"]["name"]: item["function"] for item in TOOLS}
        self.assertNotIn("priority", described["send_bot_message"]["parameters"]["properties"])
        self.assertIs(described["send_bot_message"]["parameters"]["additionalProperties"], False)
        result = self.app.send_bot_message("bot-main", sent["work_id"], {
            "dep_id": dep["id"], "text": " 加一句 ", "refs": ["artifact-nope"],
            "target_work_id": task["id"], "requires_response": True})
        self.assertEqual(result["tier"], 1)
        self.assertTrue(result["queued"])
        self.assertNotIn("injected", result)
        self.assertFalse(result["yielded"])
        line = json.loads((directory / "injects.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(line["text"], "加一句")
        self.assertEqual(line["uuid"], result["uuid"])
        self.assertNotIn("priority", line)
        self.assertNotIn("refs", line)
        self.assertNotIn(task["id"], json.dumps(line))
        # The tool path journals one row for one call id.
        runtime = Runtime(self.app, model=object())
        body = {"dep_id": dep["id"], "text": "再补一句"}
        first = runtime.durable_tool(turn, _tool("send_bot_message", body))
        again = runtime.durable_tool(turn, _tool("send_bot_message", body))
        self.assertEqual(first["uuid"], again["uuid"])
        rows = [json.loads(item) for item in (directory / "injects.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["text"] for row in rows], ["加一句", "再补一句"])
        self.assertTrue(all("priority" not in row for row in rows))


CLAUDE_STUB = r"""
import json, select, sys, pathlib
pathlib.Path("claude-argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
messages = []
while len(messages) < 2:
    ready, _, _ = select.select([sys.stdin], [], [], 8)
    if not ready:
        break
    line = sys.stdin.readline()
    if not line:
        break
    messages.append(json.loads(line))
pathlib.Path("claude-stdin.json").write_text(json.dumps(messages), encoding="utf-8")
uuids = [item.get("uuid") for item in messages]
sys.stdout.write(json.dumps({
    "type": "result", "result": "FINAL_ANSWER", "user_message_uuids": uuids}) + "\n")
sys.stdout.flush()
"""


@unittest.skipUnless(os.name == "posix", "process-group supervise is POSIX")
class StreamSuperviseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.stub = self.root / "stub_claude.py"
        self.stub.write_text(CLAUDE_STUB, encoding="utf-8")
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

    def test_running_claude_confirms_the_inject_on_the_receipt(self):
        install = self.app.execution.installations["codex-local"]
        install["adapter"] = "claude-code"
        install["argv"] = [sys.executable, str(self.stub), *ADAPTER_CATALOG["claude-code"]["argv"]]
        install["prompt_mode"] = "stdin"
        sent = self.app.send("bot-main", {"text": "task", "allow_write": True}, "stream")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {
            "prompt": "SENTINEL_PROMPT_DO_NOT_LEAK"})
        task = self.app.store.get("task", submitted["task_id"])
        task["attempt_uuid"] = "attempt-stream"
        self.app.store.put("task", task)
        task = self.app.store.get("task", task["id"])
        host = self.app.execution.hosts[task["host_id"]]
        directory = Path(self.app.execution.directory(host, task["execution_id"]))
        spec = self.app.execution.spec(task)
        self.assertEqual(spec["stdin_protocol"], "claude-stream-json")
        self.app.execution.prepare(host, str(directory), spec)
        self.directory = directory

        def work():
            with patch.object(runner, "_detect_supervision", return_value=runner.PGROUP_COOPERATIVE):
                runner.supervise(directory)

        self.thread = threading.Thread(target=work)
        self.thread.start()
        argv_path = self.workspace / "claude-argv.json"
        deadline = time.time() + 8
        while not argv_path.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(argv_path.exists())
        message = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        runner.append_inject(directory, message, "加一句")
        self.thread.join(10)
        self.assertFalse(self.thread.is_alive())
        argv = json.loads(argv_path.read_text(encoding="utf-8"))
        self.assertIn("--input-format=stream-json", argv)
        self.assertIn("--output-format=stream-json", argv)
        self.assertIn("--verbose", argv)
        self.assertIn("--replay-user-messages", argv)
        session = runner.session_id_from_execution(task["execution_id"])
        self.assertIn("--session-id=" + session, argv)
        self.assertNotIn("--fork-session", " ".join(argv))
        self.assertNotIn("SENTINEL_PROMPT_DO_NOT_LEAK", argv)
        self.assertNotIn("priority", argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("acceptEdits", argv)
        messages = json.loads((self.workspace / "claude-stdin.json").read_text(encoding="utf-8"))
        self.assertEqual(len(messages), 2)
        self.assertTrue(all("priority" not in item for item in messages))
        self.assertIn("SENTINEL_PROMPT_DO_NOT_LEAK", messages[0]["message"]["content"])
        self.assertEqual(messages[1]["message"]["content"], "加一句")
        self.assertEqual(messages[1]["uuid"], message)
        receipt = runner.inspect(directory, include_tail=True)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["interjection_tier"], 1)
        self.assertEqual(receipt["result_text"], "FINAL_ANSWER")
        self.assertEqual(receipt["tail"], "FINAL_ANSWER")
        self.assertEqual(receipt["injects"], [{"uuid": message, "delivered": True}])
        self.assertNotIn("partial", receipt["tail"])


if __name__ == "__main__":
    unittest.main()
