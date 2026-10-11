""" ACP executor transport: catalog-declared session_protocol.

One prompt round on the stdin the supervisor already owns. Permission,
elicitation, and filesystem requests are left pending. Not an interjection
tier and not a permission-to-chat bridge.
"""
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang import execution, runner
from pangpang.execution import ADAPTER_CATALOG, interjection_tier_of
from pangpang.service import Service


class CatalogTests(unittest.TestCase):
    def test_only_the_registered_name_is_a_session_protocol(self):
        self.assertIsInstance(execution._SESSION_PROTOCOLS, frozenset)
        self.assertEqual(execution._SESSION_PROTOCOLS, frozenset({"acp"}))
        self.assertEqual(runner._SESSION_PROTOCOLS, execution._SESSION_PROTOCOLS)
        row = ADAPTER_CATALOG["acp"]
        self.assertEqual(row["session_protocol"], "acp")
        self.assertEqual(row["argv"], [])
        self.assertNotIn("stdin_protocol", row)
        self.assertNotIn("resume_protocol", row)
        self.assertNotIn("interjection_tier", row)
        self.assertNotIn("--yolo", row["argv"])
        self.assertEqual(interjection_tier_of(row), 3)
        # A session protocol does not advertise tier 1 or tier 2.
        self.assertEqual(interjection_tier_of(
            {"session_protocol": "acp", "interjection_tier": 1}), 3)
        self.assertEqual(interjection_tier_of(
            {"session_protocol": "acp", "interjection_tier": 2}), 3)
        self.assertIsNone(execution.declared_session_protocol(None))
        self.assertIsNone(execution.declared_session_protocol({"session_protocol": "nope"}))
        self.assertEqual(execution.declared_session_protocol(row), "acp")
        for name in ("claude-code", "codex", "gemini", "pi"):
            self.assertNotIn("session_protocol", ADAPTER_CATALOG[name])
            self.assertIsNone(execution.session_protocol_for(name))
        self.assertEqual(execution.session_protocol_for("acp"), "acp")

    def test_acp_argv_keeps_the_prompt_off_the_list_and_refuses_silent_accept(self):
        sentinel = "SENTINEL_PROMPT_DO_NOT_LEAK"
        argv = runner.executor_argv({
            "argv": [sys.executable, "stub.py", "{prompt}"],
            "session_protocol": "acp",
        }, sentinel)
        self.assertEqual(argv, [sys.executable, "stub.py"])
        self.assertNotIn(sentinel, argv)
        self.assertNotIn("--yolo", argv)
        self.assertNotIn("--acp", argv)
        self.assertNotIn("--approve-for-me", argv)
        self.assertEqual(runner.stdin_session_protocol({"session_protocol": "acp"}, argv), "acp")
        # Undeclared executors still substitute the prompt and do not open ACP.
        plain = runner.executor_argv({"argv": ["bin", "{prompt}"]}, "hi")
        self.assertEqual(plain, ["bin", "hi"])
        self.assertIsNone(runner.stdin_session_protocol({"argv": ["bin"]}, plain))
        for flag in ("--yolo", "--approve-for-me",
                     "--dangerously-bypass-approvals-and-sandbox",
                     "--dangerously-skip-permissions",
                     "--permission-mode=bypassPermissions"):
            with self.assertRaises(ValueError) as caught:
                runner.executor_argv({
                    "argv": ["bin", flag], "session_protocol": "acp",
                }, "x")
            self.assertIn("acp_silent_accept_forbidden", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            runner.executor_argv({"argv": ["bin"], "session_protocol": "nope"}, "x")
        self.assertIn("session_protocol_unsupported", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            runner.executor_argv({
                "argv": ["bin"], "session_protocol": "acp",
                "stdin_protocol": "claude-stream-json",
            }, "x")
        self.assertIn("session_protocol_conflict", str(caught.exception))


class AcpEventTests(unittest.TestCase):
    def test_chunks_are_the_answer_and_requests_stay_pending(self):
        state = {"pending_rpc": {"id": 1, "method": "initialize"}}
        runner.apply_acp_event(state, {"id": 1, "result": {"protocolVersion": 1}})
        self.assertTrue(state["initialized"])
        state["pending_rpc"] = {"id": 2, "method": "session/new"}
        runner.apply_acp_event(state, {"id": 2, "result": {"sessionId": "sess-stub"}})
        self.assertEqual(state["acp_session_id"], "sess-stub")
        runner.apply_acp_event(state, {"method": "session/update", "params": {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "FIN"}}}})
        runner.apply_acp_event(state, {"method": "session/update", "params": {"update": {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "secret thought"}}}})
        runner.apply_acp_event(state, {"method": "session/update", "params": {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "AL_ANSWER"}}}})
        for method, ident in (
                ("session/request_permission", 9),
                ("elicitation/create", 10),
                ("fs/read_text_file", 11)):
            self.assertIsNone(runner.acp_leave_pending(state, {
                "id": ident, "method": method,
                "params": {"options": [{"kind": "allow_once", "optionId": "allow-once"}]},
            }))
        state["pending_rpc"] = {"id": 3, "method": "session/prompt"}
        runner.apply_acp_event(state, {"id": 3, "result": {"stopReason": "end_turn"}})
        self.assertEqual(state["result_text"], "FINAL_ANSWER")
        self.assertNotIn("secret thought", state["result_text"])
        self.assertEqual(state["result_seq"], 1)
        self.assertEqual(state["stop_reason"], "end_turn")
        self.assertEqual([item["method"] for item in state["acp_pending"]], [
            "session/request_permission", "elicitation/create", "fs/read_text_file"])
        blob = json.dumps(state["acp_pending"])
        self.assertNotIn("selected", blob)
        self.assertNotIn("accept", blob)
        self.assertNotIn("allow-once", blob)
        self.assertNotIn("outcome", blob)
        mismatched = {"pending_rpc": {"id": 1, "method": "initialize"}}
        runner.apply_acp_event(mismatched, {"id": 1, "result": {"protocolVersion": 99}})
        self.assertTrue(mismatched["acp_failed"])
        self.assertFalse(mismatched.get("initialized"))


ACP_STUB = r"""
import json, pathlib, select, sys, time

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

pathlib.Path("acp-argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
seen = []
session_id = "sess-stub"

def take():
    line = sys.stdin.readline()
    if not line:
        return None
    msg = json.loads(line)
    seen.append(msg)
    return msg

while True:
    msg = take()
    if msg is None:
        break
    method = msg.get("method")
    if method == "initialize" and "id" in msg:
        emit({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": False},
            "authMethods": [],
        }})
    elif method == "session/new" and "id" in msg:
        emit({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": session_id}})
    elif method == "session/prompt" and "id" in msg:
        emit({"jsonrpc": "2.0", "id": 9001, "method": "session/request_permission", "params": {
            "sessionId": session_id,
            "toolCall": {"toolCallId": "call-1", "title": "run", "kind": "execute", "status": "pending"},
            "options": [
                {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
            ],
        }})
        emit({"jsonrpc": "2.0", "id": 9002, "method": "elicitation/create", "params": {
            "mode": "form", "message": "need a token",
            "requestedSchema": {"type": "object", "properties": {}},
            "scope": {"sessionId": session_id},
        }})
        emit({"jsonrpc": "2.0", "id": 9003, "method": "fs/read_text_file", "params": {
            "sessionId": session_id, "path": "/etc/passwd",
        }})
        time.sleep(0.3)
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                break
            extra = sys.stdin.readline()
            if not extra:
                break
            seen.append(json.loads(extra))
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": session_id,
            "update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": "FINAL_ANSWER"}},
        }})
        emit({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
pathlib.Path("acp-stdin.json").write_text(json.dumps(seen), encoding="utf-8")
"""


@unittest.skipUnless(os.name == "posix", "process-group supervise is POSIX")
class AcpSuperviseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.stub = self.root / "stub_acp.py"
        self.stub.write_text(ACP_STUB, encoding="utf-8")
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

    def test_one_prompt_round_writes_a_receipt_without_approving(self):
        install = self.app.execution.installations["codex-local"]
        install["adapter"] = "acp"
        install["argv"] = [sys.executable, str(self.stub)]
        install["prompt_mode"] = "stdin"
        sent = self.app.send("bot-main", {"text": "task", "allow_write": True}, "acp")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {
            "prompt": "SENTINEL_PROMPT_DO_NOT_LEAK"})
        task = self.app.store.get("task", submitted["task_id"])
        task["attempt_uuid"] = "attempt-acp"
        task["timeout_s"] = 20
        self.app.store.put("task", task)
        task = self.app.store.get("task", task["id"])
        host = self.app.execution.hosts[task["host_id"]]
        directory = Path(self.app.execution.directory(host, task["execution_id"]))
        spec = self.app.execution.spec(task)
        self.assertEqual(spec["session_protocol"], "acp")
        self.assertNotIn("stdin_protocol", spec)
        self.assertNotIn("resume_protocol", spec)
        self.app.execution.prepare(host, str(directory), spec)
        self.directory = directory
        message = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        runner.append_inject(directory, message, "加一句")

        def work():
            with patch.object(runner, "_detect_supervision", return_value=runner.PGROUP_COOPERATIVE):
                runner.supervise(directory)

        self.thread = threading.Thread(target=work)
        self.thread.start()
        self.thread.join(20)
        self.assertFalse(self.thread.is_alive(), "ACP supervise did not finish")
        argv = json.loads((self.workspace / "acp-argv.json").read_text(encoding="utf-8"))
        self.assertNotIn("SENTINEL_PROMPT_DO_NOT_LEAK", argv)
        self.assertNotIn("--yolo", argv)
        self.assertNotIn("--acp", argv)
        self.assertNotIn("--approve-for-me", argv)
        seen = json.loads((self.workspace / "acp-stdin.json").read_text(encoding="utf-8"))
        self.assertEqual([item.get("method") for item in seen], [
            "initialize", "session/new", "session/prompt"])
        self.assertTrue(all(item.get("jsonrpc") == "2.0" for item in seen))
        caps = seen[0]["params"]["clientCapabilities"]
        self.assertEqual(seen[0]["params"]["protocolVersion"], 1)
        self.assertFalse(caps["fs"]["readTextFile"])
        self.assertFalse(caps["fs"]["writeTextFile"])
        self.assertIs(caps["terminal"], False)
        self.assertNotIn("elicitation", caps)
        self.assertEqual(seen[1]["params"]["mcpServers"], [])
        self.assertTrue(os.path.isabs(seen[1]["params"]["cwd"]))
        self.assertEqual(seen[2]["params"]["sessionId"], "sess-stub")
        self.assertEqual(seen[2]["params"]["prompt"][0]["type"], "text")
        self.assertIn("SENTINEL_PROMPT_DO_NOT_LEAK", seen[2]["params"]["prompt"][0]["text"])
        self.assertNotIn("加一句", json.dumps(seen))
        blob = json.dumps(seen)
        self.assertNotIn("allow-once", blob)
        self.assertNotIn("allow_once", blob)
        self.assertNotIn("selected", blob)
        self.assertNotIn("accept", blob)
        self.assertFalse(any(item.get("id") in (9001, 9002, 9003) for item in seen))
        receipt = runner.inspect(directory, include_tail=True)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["result_text"], "FINAL_ANSWER")
        self.assertEqual(receipt["tail"], "FINAL_ANSWER")
        self.assertEqual(receipt["session_protocol"], "acp")
        self.assertEqual(receipt["acp_session_id"], "sess-stub")
        self.assertEqual(receipt["acp_stop_reason"], "end_turn")
        self.assertEqual(receipt["acp_pending"], [
            {"method": "session/request_permission", "id": 9001},
            {"method": "elicitation/create", "id": 9002},
            {"method": "fs/read_text_file", "id": 9003},
        ])
        self.assertNotIn("interjection_tier", receipt)
        self.assertNotIn("injects", receipt)
        self.assertNotIn("resumed", receipt)
        self.assertTrue((directory / "injects.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
