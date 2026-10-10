""" permission prompts to chat. Fail closed.

The catalog key is `permission_bridge: chat`. ACP is the adapter that
declares it. A prompt is answered only after an explicit chat decision,
or denied when the wait expires. Undeclared adapters stay on today's
ignore path. No --yolo, no allow_always, no silent allow.
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
from test_acp_transport import ACP_STUB
from pangpang import runner
from pangpang.execution import ADAPTER_CATALOG, permission_bridge_for
from pangpang.runtime import TOOLS, Runtime
from pangpang.service import Service, message_inbox_meta


def _tool(name, arguments, call_id="call-permission"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


ASK = {
    "id": 9001,
    "method": "session/request_permission",
    "title": "run",
    "options": [
        {"optionId": "allow-once", "kind": "allow_once"},
        {"optionId": "allow-always", "kind": "allow_always"},
        {"optionId": "reject-once", "kind": "reject_once"},
    ],
}


class CatalogTests(unittest.TestCase):
    def test_only_the_declared_bridge_is_chat(self):
        self.assertEqual(permission_bridge_for("acp"), "chat")
        self.assertEqual(ADAPTER_CATALOG["acp"]["permission_bridge"], "chat")
        self.assertEqual(ADAPTER_CATALOG["acp"]["argv"], [])
        self.assertNotIn("--yolo", ADAPTER_CATALOG["acp"]["argv"])
        self.assertIsNone(permission_bridge_for(None))
        self.assertIsNone(permission_bridge_for("nope"))
        for name in ("qwen-claude", "claude-code", "codex", "gemini", "pi"):
            self.assertNotIn("permission_bridge", ADAPTER_CATALOG[name])
            self.assertIsNone(permission_bridge_for(name))

    def test_frame_selects_allow_once_or_denies(self):
        allowed = runner.acp_permission_frame(ASK, "approve")
        self.assertEqual(allowed["result"]["outcome"], {"outcome": "selected", "optionId": "allow-once"})
        self.assertNotIn("method", allowed)
        blob = json.dumps(allowed)
        self.assertNotIn("allow_always", blob)
        self.assertNotIn("allow-always", blob)
        self.assertNotIn("acceptForSession", blob)
        always = dict(ASK, options=[{"optionId": "allow-always", "kind": "allow_always"}])
        denied = runner.acp_permission_frame(always, "approve")
        self.assertEqual(denied["result"]["outcome"], {"outcome": "cancelled"})
        self.assertEqual(runner.acp_permission_frame(ASK, "deny")["result"]["outcome"],
                         {"outcome": "cancelled"})
        elicited = runner.acp_permission_frame(
            {"id": 10, "method": "elicitation/create", "options": []}, "approve")
        self.assertEqual(elicited["result"]["outcome"], {"outcome": "declined"})
        self.assertNotIn("accepted", json.dumps(elicited))
        self.assertIsNone(runner.acp_permission_frame(
            {"id": 11, "method": "fs/read_text_file"}, "approve"))

    def test_unanswered_rows_do_not_grant_allow(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        runner.append_permission_ask(directory, ASK)
        rows = runner.read_permission_rows(directory)
        self.assertFalse(runner.permission_grants_allow(rows, "9001"))
        # A second read is the replay. Still not an approval.
        self.assertFalse(runner.permission_grants_allow(runner.read_permission_rows(directory), 9001))
        with self.assertRaises(ValueError):
            runner.append_permission_decision(directory, "9001", "approve", source="timeout")
        stored = runner.append_permission_decision(directory, "9001", "deny", source="timeout")
        self.assertEqual(stored["source"], "timeout")
        self.assertFalse(runner.permission_grants_allow(runner.read_permission_rows(directory), "9001"))
        # A later chat approve cannot upgrade the timeout denial.
        again = runner.append_permission_decision(
            directory, "9001", "approve", source="chat", message_id="msg-1")
        self.assertEqual(again["decision"], "deny")
        self.assertEqual(again["source"], "timeout")
        self.assertFalse(runner.permission_grants_allow(runner.read_permission_rows(directory), "9001"))
        vague = {"op": "decide", "request_id": "7", "decision": "approve"}
        self.assertEqual(runner.effective_permission_decision(vague), "deny")
        self.assertIsNone(runner.classify_permission_utterance("同意，但是先别跑"))
        self.assertIsNone(runner.classify_permission_utterance(""))
        self.assertEqual(runner.classify_permission_utterance("同意"), "approve")
        self.assertEqual(runner.classify_permission_utterance("拒绝"), "deny")
        self.assertEqual(runner.classify_permission_utterance("同意 9001", request_id="9001"), "approve")

    def test_vendors_without_the_bridge_still_ignore_prompts(self):
        state = {"result_text": None, "confirmed": set()}
        runner.apply_stream_event(state, {"type": "control_request", "request_id": "r1", "request": {
            "subtype": "can_use_tool", "tool_name": "Bash"}})
        self.assertNotIn("acp_requests", state)
        self.assertNotIn("permission_decisions", state)
        runner.apply_codex_event(state, {
            "id": 9, "method": "item/commandExecution/requestApproval", "params": {}})
        self.assertNotIn("decision", state)
        self.assertEqual(message_inbox_meta("permission", fyi=True), (True, "fyi"))


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.app = Service(config(self.workspace), self.root / "data")
        self.app.store.put("probe", {
            "id": "codex-local", "available": True,
            "supervision": runner.PGROUP_COOPERATIVE})
        self.runtime = Runtime(self.app, model=object())
        self._n = 0

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _task(self, utterance, *, adapter="acp"):
        self._n += 1
        n = str(self._n)
        self.app.execution.installations["codex-local"]["adapter"] = adapter
        sent = self.app.send("bot-main", {"text": "task", "allow_write": True}, "perm-task-" + n)
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "do the thing"})
        task = self.app.store.get("task", submitted["task_id"])
        task["status"] = "running"
        self.app.store.put("task", task)
        host = self.app.execution.hosts[task["host_id"]]
        directory = Path(self.app.execution.directory(host, task["execution_id"]))
        directory.mkdir(parents=True, exist_ok=True)
        reply = self.app.send("bot-main", {"text": utterance, "work_id": sent["work_id"]}, "perm-reply-" + n)
        turn = self.app.store.get("turn", reply["turn_id"])
        return sent, task, submitted, directory, turn

    def test_ambiguous_and_replay_do_not_approve(self):
        described = {item["function"]["name"]: item["function"] for item in TOOLS}
        self.assertEqual(described["resolve_permission"]["parameters"]["properties"]["decision"]["enum"],
                         ["approve", "deny"])
        _, _, submitted, directory, turn = self._task("看看这个")
        runner.append_permission_ask(directory, ASK)
        call = _tool("resolve_permission", {
            "dep_id": submitted["dependency_id"], "request_id": "9001", "decision": "approve"})
        first = self.runtime.durable_tool(turn, call)
        again = self.runtime.durable_tool(turn, call)
        self.assertEqual(first["error"], "permission_undecided")
        self.assertEqual(again, first)
        rows = runner.read_permission_rows(directory)
        self.assertIsNone(runner.permission_decision_row(rows, "9001"))
        self.assertFalse(runner.permission_grants_allow(rows, "9001"))
        # The user's words and the tool argument disagree. Still no allow.
        _, _, submitted2, directory2, turn2 = self._task("同意")
        runner.append_permission_ask(directory2, ASK)
        mismatched = self.runtime.durable_tool(turn2, _tool("resolve_permission", {
            "dep_id": submitted2["dependency_id"], "request_id": "9001", "decision": "deny"},
            "call-mismatch"))
        self.assertEqual(mismatched["error"], "permission_undecided")
        self.assertFalse(runner.permission_grants_allow(runner.read_permission_rows(directory2), "9001"))

    def test_explicit_approve_is_once_and_replay_does_not_write_again(self):
        # Announce first. The approving utterance has to be created after that.
        sent, task, submitted, directory, _turn = self._task("先看一下")
        runner.append_permission_ask(directory, ASK)
        self.assertTrue(self.app.announce_permission_prompts(task))
        time.sleep(0.01)
        reply = self.app.send("bot-main", {"text": "同意", "work_id": sent["work_id"]}, "perm-approve-after")
        turn = self.app.store.get("turn", reply["turn_id"])
        body = {"dep_id": submitted["dependency_id"], "request_id": "9001", "decision": "approve"}
        first = self.runtime.durable_tool(turn, _tool("resolve_permission", body))
        again = self.runtime.durable_tool(turn, _tool("resolve_permission", body))
        self.assertEqual(first["resolved"], True)
        self.assertEqual(first["decision"], "approve")
        self.assertFalse(first["replayed"])
        self.assertEqual(again["decision"], "approve")
        rows = [row for row in runner.read_permission_rows(directory) if row.get("op") == "decide"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "chat")
        self.assertEqual(rows[0]["decision"], "approve")
        self.assertTrue(rows[0]["message_id"])
        self.assertTrue(runner.permission_grants_allow(runner.read_permission_rows(directory), "9001"))

    def test_approve_before_the_request_does_not_grant(self):
        _, task, submitted, directory, turn = self._task("同意")
        runner.append_permission_ask(directory, ASK)
        self.assertTrue(self.app.announce_permission_prompts(task))
        body = {"dep_id": submitted["dependency_id"], "request_id": "9001", "decision": "approve"}
        result = self.runtime.durable_tool(turn, _tool("resolve_permission", body, "call-early"))
        self.assertEqual(result["error"], "permission_undecided")
        rows = runner.read_permission_rows(directory)
        self.assertIsNone(runner.permission_decision_row(rows, "9001"))
        self.assertFalse(runner.permission_grants_allow(rows, "9001"))

    def test_allow_always_and_undeclared_and_timeout_do_not_allow(self):
        _, _, submitted, directory, turn = self._task("同意")
        always = dict(ASK, options=[{"optionId": "allow-always", "kind": "allow_always"}])
        runner.append_permission_ask(directory, always)
        refused = self.runtime.durable_tool(turn, _tool("resolve_permission", {
            "dep_id": submitted["dependency_id"], "request_id": "9001", "decision": "approve"}))
        self.assertEqual(refused["error"], "permission_not_allow_once")
        self.assertFalse(runner.permission_grants_allow(runner.read_permission_rows(directory), "9001"))

        _, _, submitted_u, directory_u, turn_u = self._task("同意", adapter="codex")
        runner.append_permission_ask(directory_u, ASK)
        undeclared = self.runtime.durable_tool(turn_u, _tool("resolve_permission", {
            "dep_id": submitted_u["dependency_id"], "request_id": "9001", "decision": "approve"},
            "call-undeclared"))
        self.assertEqual(undeclared["error"], "permission_bridge_undeclared")
        self.assertIsNone(runner.permission_decision_row(runner.read_permission_rows(directory_u), "9001"))

        _, _, submitted_t, directory_t, turn_t = self._task("同意", adapter="acp")
        runner.append_permission_ask(directory_t, ASK)
        runner.append_permission_decision(directory_t, "9001", "deny", source="timeout")
        late = self.runtime.durable_tool(turn_t, _tool("resolve_permission", {
            "dep_id": submitted_t["dependency_id"], "request_id": "9001", "decision": "approve"},
            "call-late"))
        self.assertEqual(late["error"], "permission_already_decided")
        self.assertEqual(late["decision"], "deny")
        self.assertFalse(runner.permission_grants_allow(runner.read_permission_rows(directory_t), "9001"))

    def test_announce_posts_one_chat_notice(self):
        _, task, _, directory, turn = self._task("先不用")
        runner.append_permission_ask(directory, ASK)
        self.assertTrue(self.app.announce_permission_prompts(task))
        self.assertFalse(self.app.announce_permission_prompts(self.app.store.get("task", task["id"])))
        notes = [m for m in self.app.store.find("message", work_id=task["work_id"])
                 if m.get("kind") == "permission"]
        self.assertEqual(len(notes), 1)
        self.assertIn("9001", notes[0]["content"])
        self.assertIn("同意", notes[0]["content"])
        self.assertIn("拒绝", notes[0]["content"])
        self.assertEqual(notes[0]["wake_intent"], "fyi")
        stored = self.app.store.get("task", task["id"])
        self.assertEqual(stored["permission_asks"], [{
            "request_id": "9001", "title": "run", "method": "session/request_permission"}])
        from pangpang.context import projection
        _system = projection(self.app, turn)[0]["content"]
        packet = json.loads(_system[_system.index("{"):])
        dep = next(item for item in packet["dependencies"] if item.get("status") == "pending")
        self.assertEqual(dep["target"]["permission_asks"][0]["request_id"], "9001")
        self.assertIn("resolve_permission", projection(self.app, turn)[0]["content"])


ACP_WAIT_STUB = r"""
import json, pathlib, sys

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

try:
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
                    {"optionId": "allow-always", "name": "Always", "kind": "allow_always"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                ],
            }})
            answer = None
            while True:
                extra = sys.stdin.readline()
                if not extra:
                    break
                parsed = json.loads(extra)
                seen.append(parsed)
                if parsed.get("id") == 9001 and "result" in parsed:
                    answer = parsed
                    break
            text = "NO_DECISION"
            outcome = ((answer or {}).get("result") or {}).get("outcome") or {}
            if outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow-once":
                text = "ALLOWED"
            elif outcome.get("outcome") == "cancelled":
                text = "DENIED"
            elif answer is not None:
                text = "OTHER"
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": text}},
            }})
            emit({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
            break
finally:
    pathlib.Path("acp-stdin.json").write_text(json.dumps(seen), encoding="utf-8")
"""


@unittest.skipUnless(os.name == "posix", "process-group supervise is POSIX")
class AcpPermissionSuperviseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.stub = self.root / "stub_acp.py"
        self.app = Service(config(self.workspace), self.root / "data")
        self.app.store.put("probe", {
            "id": "codex-local", "available": True,
            "supervision": runner.PGROUP_COOPERATIVE})
        self.runtime = Runtime(self.app, model=object())
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

    def _start(self, stub, *, bridge=True, permission_timeout_s=None):
        self.stub.write_text(stub, encoding="utf-8")
        install = self.app.execution.installations["codex-local"]
        install["adapter"] = "acp"
        install["argv"] = [sys.executable, str(self.stub)]
        install["prompt_mode"] = "stdin"
        sent = self.app.send("bot-main", {"text": "task", "allow_write": True}, "acp-perm")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {
            "prompt": "SENTINEL_PROMPT_DO_NOT_LEAK"})
        task = self.app.store.get("task", submitted["task_id"])
        task["attempt_uuid"] = "attempt-perm"
        task["timeout_s"] = 20
        task["status"] = "running"
        self.app.store.put("task", task)
        task = self.app.store.get("task", task["id"])
        host = self.app.execution.hosts[task["host_id"]]
        directory = Path(self.app.execution.directory(host, task["execution_id"]))
        spec = self.app.execution.spec(task)
        self.assertEqual(spec.get("permission_bridge"), "chat")
        if not bridge:
            spec.pop("permission_bridge", None)
        if permission_timeout_s is not None:
            spec["permission_timeout_s"] = permission_timeout_s
        self.app.execution.prepare(host, str(directory), spec)
        self.directory = directory

        def work():
            with patch.object(runner, "_detect_supervision", return_value=runner.PGROUP_COOPERATIVE):
                runner.supervise(directory)

        self.thread = threading.Thread(target=work)
        self.thread.start()
        return sent, task, submitted

    def _wait_ask(self):
        deadline = time.time() + 8
        while time.time() < deadline:
            rows = runner.read_permission_rows(self.directory)
            asks = [row for row in rows if row.get("op") == "ask"]
            if asks:
                return asks[0]
            time.sleep(0.05)
        self.fail("permission ask was not surfaced")

    def _finish(self):
        self.thread.join(20)
        self.assertFalse(self.thread.is_alive(), "ACP supervise did not finish")
        argv = json.loads((self.workspace / "acp-argv.json").read_text(encoding="utf-8"))
        seen = json.loads((self.workspace / "acp-stdin.json").read_text(encoding="utf-8"))
        receipt = runner.inspect(self.directory, include_tail=True)
        return argv, seen, receipt

    def _decide(self, sent, submitted, utterance, decision):
        reply = self.app.send("bot-main", {
            "text": utterance, "work_id": sent["work_id"]}, "acp-" + utterance)
        turn = self.app.store.get("turn", reply["turn_id"])
        return self.runtime.durable_tool(turn, _tool("resolve_permission", {
            "dep_id": submitted["dependency_id"], "request_id": "9001", "decision": decision}))

    def test_approve_writes_allow_once_on_the_same_stdin(self):
        sent, task, submitted = self._start(ACP_WAIT_STUB)
        ask = self._wait_ask()
        self.assertEqual(ask["request_id"], "9001")
        self.assertTrue(self.app.announce_permission_prompts(task))
        notes = [m for m in self.app.store.find("message", work_id=sent["work_id"])
                 if m.get("kind") == "permission"]
        self.assertEqual(len(notes), 1)
        time.sleep(0.01)
        result = self._decide(sent, submitted, "同意", "approve")
        self.assertEqual(result["decision"], "approve")
        argv, seen, receipt = self._finish()
        self.assertNotIn("--yolo", argv)
        self.assertNotIn("--approve-for-me", argv)
        self.assertNotIn("SENTINEL_PROMPT_DO_NOT_LEAK", argv)
        answer = next(item for item in seen if item.get("id") == 9001)
        self.assertEqual(answer["result"]["outcome"], {"outcome": "selected", "optionId": "allow-once"})
        self.assertNotIn("method", answer)
        blob = json.dumps(seen)
        self.assertNotIn("allow_always", blob)
        self.assertNotIn("allow-always", blob)
        self.assertNotIn("acceptForSession", blob)
        self.assertEqual(receipt["result_text"], "ALLOWED")
        self.assertEqual(receipt["permission_decisions"], [{
            "request_id": "9001", "method": "session/request_permission",
            "decision": "approve", "source": "chat", "outcome": "selected",
            "option_id": "allow-once"}])
        pending = receipt.get("acp_pending") or []
        self.assertNotIn("9001", [str(item.get("id")) for item in pending])
        self.assertTrue(all(item.get("outcome") != "selected" or item.get("source") == "chat"
                            for item in receipt["permission_decisions"]))

    def test_deny_cancels_without_selecting_allow(self):
        sent, task, submitted = self._start(ACP_WAIT_STUB)
        self._wait_ask()
        self.assertTrue(self.app.announce_permission_prompts(task))
        time.sleep(0.01)
        result = self._decide(sent, submitted, "拒绝", "deny")
        self.assertEqual(result["decision"], "deny")
        _, seen, receipt = self._finish()
        answer = next(item for item in seen if item.get("id") == 9001)
        self.assertEqual(answer["result"]["outcome"], {"outcome": "cancelled"})
        self.assertNotIn("selected", json.dumps(answer))
        self.assertNotIn("allow", json.dumps(answer))
        self.assertEqual(receipt["result_text"], "DENIED")
        self.assertEqual(receipt["permission_decisions"][0]["decision"], "deny")
        self.assertEqual(receipt["permission_decisions"][0]["source"], "chat")
        self.assertEqual(receipt["permission_decisions"][0]["outcome"], "cancelled")
        self.assertNotIn("option_id", receipt["permission_decisions"][0])

    def test_timeout_denies_and_replay_stays_denied(self):
        self._start(ACP_WAIT_STUB, permission_timeout_s=0.8)
        self._wait_ask()
        _, seen, receipt = self._finish()
        answer = next(item for item in seen if item.get("id") == 9001)
        self.assertEqual(answer["result"]["outcome"], {"outcome": "cancelled"})
        blob = json.dumps(seen)
        self.assertNotIn("allow-once", blob)
        self.assertNotIn("allow_once", blob)
        self.assertNotIn("allow_always", blob)
        self.assertNotIn("selected", blob)
        self.assertNotIn("acceptForSession", blob)
        self.assertEqual(receipt["result_text"], "DENIED")
        decision = receipt["permission_decisions"][0]
        self.assertEqual(decision["decision"], "deny")
        self.assertEqual(decision["source"], "timeout")
        self.assertEqual(decision["outcome"], "cancelled")
        rows = runner.read_permission_rows(self.directory)
        self.assertFalse(runner.permission_grants_allow(rows, "9001"))
        self.assertFalse(runner.permission_grants_allow(runner.read_permission_rows(self.directory), "9001"))
        pending = receipt.get("acp_pending") or []
        self.assertNotIn("9001", [str(item.get("id")) for item in pending])

    def test_undeclared_path_does_not_answer(self):
        self._start(ACP_STUB, bridge=False)
        _, seen, receipt = self._finish()
        self.assertFalse((self.directory / "permissions.jsonl").exists())
        self.assertEqual([item.get("method") for item in seen], [
            "initialize", "session/new", "session/prompt"])
        blob = json.dumps(seen)
        self.assertNotIn("selected", blob)
        self.assertNotIn("allow-once", blob)
        self.assertNotIn("allow_once", blob)
        self.assertNotIn("accept", blob)
        self.assertFalse(any(item.get("id") in (9001, 9002, 9003) for item in seen))
        self.assertEqual(receipt["acp_pending"], [
            {"method": "session/request_permission", "id": 9001},
            {"method": "elicitation/create", "id": 9002},
            {"method": "fs/read_text_file", "id": 9003},
        ])
        self.assertNotIn("permission_decisions", receipt)
        self.assertEqual(receipt["result_text"], "FINAL_ANSWER")


if __name__ == "__main__":
    unittest.main()
