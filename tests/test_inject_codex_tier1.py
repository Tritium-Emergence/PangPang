""" Codex tier 1: one user sentence into the running task.

The Bot path is still send_bot_message. Codex declares codex-app-server.
`codex exec` with a closed stdin is not this channel. Undeclared protocols
stay on tier 3. The runner does not approve permission requests.
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
from test_unknown_adapter_startup import _removed_adapter_name
from pangpang import runner
from pangpang.execution import ADAPTER_CATALOG, CODEX_APP_SERVER, interjection_tier_of
from pangpang.runtime import TOOLS
from pangpang.service import INJECT_UNSUPPORTED_ADVICE, Service
from pangpang.store import Fault


def _tool(name, arguments, call_id="call-inject"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


class CapabilityTests(unittest.TestCase):
    def test_codex_declares_app_server_and_undeclared_stays_tier_3(self):
        self.assertEqual(CODEX_APP_SERVER, "codex-app-server")
        self.assertEqual(runner.CODEX_APP_SERVER, "codex-app-server")
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG["codex"]), 1)
        self.assertEqual(ADAPTER_CATALOG["codex"]["stdin_protocol"], "codex-app-server")
        # Stored invocation is still exec. The runner rewrites it at launch.
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"],
                         ["exec", "--sandbox", "workspace-write", "-"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox",
                         ADAPTER_CATALOG["codex"]["argv"])
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG.get(_removed_adapter_name())), 3)
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG["pi"]), 3)
        self.assertEqual(interjection_tier_of(
            {"interjection_tier": 1, "stdin_protocol": "gemini-stream"}), 3)
        self.assertEqual(interjection_tier_of(
            {"interjection_tier": 1, "stdin_protocol": "claude-stream-json"}), 1)
        self.assertNotIn("codex", {item["function"]["name"] for item in TOOLS})
        described = {item["function"]["name"]: item["function"] for item in TOOLS}
        self.assertNotIn("priority", described["send_bot_message"]["parameters"]["properties"])

    def test_exec_becomes_app_server_and_keeps_stored_sandbox_values(self):
        execution_id = "exec-" + "ab" * 16
        sentinel = "SENTINEL_PROMPT_DO_NOT_LEAK"
        spec = {
            "argv": ["codex", "exec", "--sandbox", "workspace-write",
                     "--ask-for-approval", "on-request", "-"],
            "execution_id": execution_id,
            "stdin_protocol": "codex-app-server",
        }
        argv = runner.executor_argv(spec, sentinel)
        self.assertEqual(argv[0], "codex")
        self.assertIn("app-server", argv)
        self.assertNotIn("exec", argv)
        self.assertNotIn("-", argv)
        self.assertIn("--listen=stdio://", argv)
        self.assertNotIn(sentinel, argv)
        self.assertNotIn("fork", argv)
        self.assertNotIn("--approve-for-me", argv)
        self.assertTrue(all("priority" not in part for part in argv))
        # The flag tokens are not renamed on the process list. app-server
        # rejects --sandbox after the subcommand and ignores it before, so the
        # same strings travel on thread/start instead.
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("workspace-write", argv)
        _launch, params = runner.codex_app_server_plan(spec["argv"])
        self.assertEqual(params["sandbox"], "workspace-write")
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertNotIn("priority", params)
        self.assertNotIn("approvalsReviewer", params)
        bare = runner.codex_app_server_plan(["codex", "exec", "-"])
        self.assertEqual(bare[0], ["codex", "app-server", "--listen=stdio://"])
        self.assertEqual(bare[1], {"approvalPolicy": "never"})
        self.assertNotIn("sandbox", bare[1])
        # A foreign argv is not rewritten, so the closed-stdin stub path stays.
        stub = [sys.executable, "-c", "print(1)"]
        self.assertIsNone(runner.codex_app_server_plan(stub))
        self.assertEqual(runner.executor_argv(
            {"argv": stub, "stdin_protocol": "codex-app-server"}, sentinel), stub)
        self.assertIsNone(runner.stdin_session_protocol(
            {"stdin_protocol": "codex-app-server"}, stub))

    def test_fork_is_refused_and_bypass_is_not_an_accept(self):
        spec = {"argv": ["codex", "fork", "--last"], "execution_id": "exec-" + "cd" * 16,
                "stdin_protocol": "codex-app-server"}
        with self.assertRaises(ValueError):
            runner.executor_argv(spec, "prompt")
        launch, params = runner.codex_app_server_plan(
            ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--approve-for-me", "-"])
        self.assertEqual(params["sandbox"], "danger-full-access")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertNotIn("--approve-for-me", launch)
        self.assertNotIn("accept", json.dumps(params))
        with self.assertRaises(ValueError):
            runner.codex_app_server_plan(["codex", "app-server", "--listen", "ws://127.0.0.1:9"])

    def test_default_plan_is_workspace_write_and_outside_writes_stay_unprivileged(self):
        default = ["codex", *ADAPTER_CATALOG["codex"]["argv"]]
        launch, params = runner.codex_app_server_plan(default)
        self.assertEqual(params["sandbox"], "workspace-write")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertNotIn("writableRoots", params)
        self.assertNotIn("writable_roots", params)
        self.assertNotIn("--sandbox", launch)
        self.assertNotIn("workspace-write", launch)
        self.assertNotEqual(params.get("sandbox"), "danger-full-access")
        # --add-dir would widen the writable set past the task workspace.
        # The plan drops it, so that write is not granted.
        outside = runner.codex_app_server_plan([
            "codex", "exec", "--sandbox", "workspace-write",
            "--add-dir", "/outside-workspace", "-"])
        self.assertEqual(outside[1]["sandbox"], "workspace-write")
        self.assertNotIn("--add-dir", outside[0])
        self.assertNotIn("/outside-workspace", outside[0])
        self.assertNotIn("writableRoots", outside[1])
        self.assertNotEqual(outside[1].get("sandbox"), "danger-full-access")
        for argv in (
            default,
            ["codex", "exec", "-"],
            ["codex", "exec", "--sandbox", "read-only", "-"],
            ["codex", "exec", "--sandbox", "workspace-write", "-"],
            ["codex", "app-server"],
        ):
            planned = runner.codex_app_server_plan(argv)
            self.assertNotEqual(planned[1].get("sandbox"), "danger-full-access", argv)

    def test_delivery_is_client_id_and_approval_requests_are_not_answered(self):
        state = {"result_text": None, "confirmed": set()}
        runner.apply_codex_event(state, {"id": 1, "result": {"userAgent": "stub"}})
        self.assertIsNone(state.get("thread_id"))
        runner.apply_codex_event(state, {
            "id": 2, "result": {"thread": {"id": "thr_server", "forkedFromId": None}}})
        runner.apply_codex_event(state, {
            "method": "thread/started",
            "params": {"thread": {"id": "thr_forked", "forkedFromId": "thr_server"}}})
        self.assertEqual(state["thread_id"], "thr_server")
        runner.apply_codex_event(state, {
            "id": 3, "result": {"turn": {"id": "turn_1", "status": "inProgress"}}})
        self.assertTrue(state["turn_active"])
        # The steer response names a turn. That is not delivery.
        runner.apply_codex_event(state, {"id": 4, "result": {"turnId": "turn_1"}})
        self.assertEqual(state["confirmed"], set())
        runner.apply_codex_event(state, {
            "id": 9, "method": "item/commandExecution/requestApproval",
            "params": {"itemId": "c1", "threadId": "thr_server", "turnId": "turn_1"}})
        self.assertEqual(state["confirmed"], set())
        self.assertNotIn("decision", state)
        runner.apply_codex_event(state, {"method": "item/completed", "params": {"item": {
            "type": "userMessage", "clientId": "inject-uuid", "content": []}}})
        self.assertEqual(state["confirmed"], {"inject-uuid"})
        runner.apply_codex_event(state, {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "text": "FINAL_ANSWER", "phase": "final_answer"}}})
        runner.apply_codex_event(state, {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "text": "later commentary", "phase": "commentary"}}})
        self.assertEqual(state["result_text"], "FINAL_ANSWER")
        runner.apply_codex_event(state, {"method": "turn/completed", "params": {"turn": {
            "id": "turn_1", "status": "completed"}}})
        self.assertFalse(state["turn_active"])
        self.assertEqual(state["result_seq"], 1)
        user_input, message_id = runner.codex_turn_input("加一句", "inject-uuid")
        self.assertEqual(user_input, [{"type": "text", "text": "加一句"}])
        self.assertEqual(message_id, "inject-uuid")
        self.assertNotIn("priority", user_input[0])


BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"

BYPASS_STUB = r"""
import json, os, pathlib, sys
pathlib.Path("bypass-ran.json").write_text(json.dumps({
    "argv": sys.argv,
    "cwd": os.getcwd(),
}), encoding="utf-8")
sys.stdin.read()
print("ok")
"""


class CustomAdapterBypassTests(unittest.TestCase):
    def setUp(self):
        from pangpang import adapters_file
        self.adapters_file = adapters_file
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.closers = []
        self.addCleanup(self._close)
        self.addCleanup(self._drop_custom_row)

    def _close(self):
        for app in reversed(self.closers):
            try:
                app.store.close()
            except Exception:
                pass

    def _drop_custom_row(self):
        ADAPTER_CATALOG.pop("open-exec", None)
        self.adapters_file._merged_names.discard("open-exec")

    @staticmethod
    def _restore_path(old):
        if old is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old

    def _track(self, app):
        self.closers.append(app)
        return app

    def test_adapters_file_runs_the_bypass_and_chat_cannot_set_it(self):
        root = Path(self.tmp.name).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        data = root / "data"
        data.mkdir()
        stub = root / "stub.py"
        stub.write_text(BYPASS_STUB, encoding="utf-8")
        bindir = root / "bin"
        bindir.mkdir()
        launcher = bindir / "open-exec"
        launcher.symlink_to(sys.executable)
        old_path = os.environ.get("PATH")
        os.environ["PATH"] = str(bindir) + os.pathsep + (old_path or "")
        self.addCleanup(self._restore_path, old_path)
        row = {"binary": "open-exec", "argv": [str(stub), BYPASS_FLAG],
               "concurrency": 1, "priority": 80,
               "use_when": "Run the recorded command with the sandbox bypass flag."}
        path = data / "adapters.json"
        path.write_text(json.dumps({"open-exec": row}), encoding="utf-8")
        path.chmod(0o644)
        from pangpang.onboarding import empty_config
        app = self._track(Service(empty_config(), data))
        self.assertEqual(ADAPTER_CATALOG["open-exec"]["argv"], [str(stub), BYPASS_FLAG])
        self.assertNotIn("stdin_protocol", ADAPTER_CATALOG["open-exec"])
        registered = app.ensure_local_host(str(workspace), discover=True)
        self.assertIn("inst-local-open-exec", registered["installations"])
        inst = app.execution.installations["inst-local-open-exec"]
        resolved = str(launcher)
        self.assertEqual(inst["argv"], [resolved, str(stub), BYPASS_FLAG])
        self.assertEqual(Path(inst["cwd"]).resolve(), workspace)
        app.probe("inst-local-open-exec")
        sent = app.send("bot-main", {"text": "run", "allow_write": True}, "bypass")
        submitted = app.submit_task("bot-main", sent["work_id"], {
            "prompt": "go", "installation_id": "inst-local-open-exec"})
        self.assertEqual(submitted["status"], "awaiting_approval")
        task = app.store.get("task", submitted["task_id"])
        self.assertEqual(Path(task["cwd"]).resolve(), workspace)
        spec = app.execution.spec(task)
        self.assertNotIn("stdin_protocol", spec)
        launched = runner.executor_argv(spec, task["prompt"])
        self.assertEqual(launched, [str(root / "bin" / "open-exec"), str(stub), BYPASS_FLAG])
        self.assertNotIn("app-server", launched)
        app.approve(task["id"], {"spec_digest": task["spec_digest"]})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            app.task_step()
            task = app.store.get("task", task["id"])
            if task["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                break
            time.sleep(0.05)
        self.assertEqual(task["status"], "succeeded", task.get("receipt") or task.get("error"))
        recorded = json.loads((workspace / "bypass-ran.json").read_text(encoding="utf-8"))
        self.assertIn(BYPASS_FLAG, recorded["argv"])
        self.assertNotIn("app-server", recorded["argv"])
        self.assertEqual(Path(recorded["cwd"]).resolve(), workspace)
        self.assertEqual(Path(recorded["cwd"]).resolve(), Path(task["cwd"]).resolve())

        neg_root = root / "neg"
        neg_root.mkdir()
        neg = self._track(Service(config(neg_root), root / "neg-data"))
        work = neg.send("bot-main", {"text": "run", "allow_write": True}, "chat")["work_id"]
        stored = list(neg.execution.installations["codex-local"]["argv"])
        for body in (
            {"installation_id": "codex-local", "extra_argv": [BYPASS_FLAG]},
            {"installation_id": "codex-local", "extra_argv": ["--yolo"]},
            {"host_id": "local", "adapter": "codex", "profile": "bypass",
             "extra_argv": [BYPASS_FLAG]},
        ):
            with self.assertRaises(Fault) as ctx:
                neg.propose_executor("bot-main", work, body)
            self.assertEqual(ctx.exception.code, "invalid_extra_argv")
        with self.assertRaises(Fault) as ctx:
            neg.propose_executor("bot-main", work, {
                "installation_id": "codex-local", "argv": [stored[0], "--yolo"]})
        self.assertEqual(ctx.exception.code, "unsupported_installation_field")
        with self.assertRaises(Fault) as ctx:
            neg.configure_installation("codex-local", {"extra_argv": ["--yolo"]})
        self.assertEqual(ctx.exception.code, "invalid_extra_argv")
        self.assertEqual(neg.execution.installations["codex-local"]["argv"], stored)
        staged = neg.propose_executor("bot-main", work, {
            "host_id": "local", "adapter": "codex", "profile": "plain",
            "cwd": str(neg_root)})
        request = neg.store.get("setup_request", staged["staged"]["request_id"])
        self.assertEqual(request["proposal"]["command"][1:], ADAPTER_CATALOG["codex"]["argv"])
        self.assertNotIn(BYPASS_FLAG, request["proposal"]["command"])
        approved = neg.onboarding.approve(request["id"], {"endpoint": ""})
        created = neg.execution.installations[approved["installation_id"]]
        self.assertEqual(created["argv"][1:], ADAPTER_CATALOG["codex"]["argv"])
        self.assertNotIn(BYPASS_FLAG, created["argv"])
        self.assertNotIn("--yolo", created["argv"])


class MailboxTests(unittest.TestCase):
    def test_mailbox_rejects_priority(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        message = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        with self.assertRaises(ValueError):
            runner.accept_inject(directory, {"uuid": message, "text": "加一句", "priority": "now"})
        self.assertFalse((directory / "injects.jsonl").exists())


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
        self.assertFalse((directory / "injects.jsonl").exists())

    def test_undeclared_executor_advises_tier_3(self):
        sent, _, dep, _, directory = self._running_task("tier3")
        self.app.execution.installations["codex-local"]["adapter"] = "pi"
        with self.assertRaises(Fault) as caught:
            self.app.send_bot_message("bot-main", sent["work_id"], {
                "dep_id": dep["id"], "text": "加一句"})
        self.assertEqual(caught.exception.code, "interjection_unsupported")
        self.assertEqual(caught.exception.advice, INJECT_UNSUPPORTED_ADVICE)
        self.assertIn("cancel_dependency", caught.exception.advice)
        self.assertFalse((directory / "injects.jsonl").exists())

    def test_codex_inject_is_user_text_only(self):
        sent, task, dep, turn, directory = self._running_task("codex")
        self.assertEqual(self.app.execution.installations["codex-local"]["adapter"], "codex")
        result = self.app.send_bot_message("bot-main", sent["work_id"], {
            "dep_id": dep["id"], "text": " 加一句 ", "refs": ["artifact-nope"],
            "target_work_id": task["id"], "requires_response": True, "priority": "now"})
        self.assertEqual(result["tier"], 1)
        self.assertTrue(result["queued"])
        self.assertNotIn("injected", result)
        self.assertFalse(result["yielded"])
        line = json.loads((directory / "injects.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(set(line), {"op", "uuid", "text"})
        self.assertEqual(line["text"], "加一句")
        self.assertNotIn("priority", line)
        runtime = __import__("pangpang.runtime", fromlist=["Runtime"]).Runtime(self.app, model=object())
        body = {"dep_id": dep["id"], "text": "再补一句"}
        first = runtime.durable_tool(turn, _tool("send_bot_message", body))
        again = runtime.durable_tool(turn, _tool("send_bot_message", body))
        self.assertEqual(first["uuid"], again["uuid"])
        rows = [json.loads(item) for item in (directory / "injects.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["text"] for row in rows], ["加一句", "再补一句"])
        spec = self.app.execution.spec(task)
        self.assertEqual(spec["stdin_protocol"], "codex-app-server")


CODEX_STUB = r"""
import json, pathlib, sys
pathlib.Path("codex-argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
seen = []
thread_id = "thr_from_server"
turn_id = "turn_from_server"

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

while True:
    line = sys.stdin.readline()
    if not line:
        break
    msg = json.loads(line)
    seen.append(msg)
    method = msg.get("method")
    if method == "initialize" and "id" in msg:
        emit({"id": msg["id"], "result": {"userAgent": "stub"}})
    elif method == "thread/start" and "id" in msg:
        emit({"id": msg["id"], "result": {"thread": {"id": thread_id, "forkedFromId": None}}})
        emit({"method": "thread/started", "params": {"thread": {"id": thread_id, "forkedFromId": None}}})
    elif method == "turn/start" and "id" in msg:
        emit({"id": msg["id"], "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        emit({"method": "turn/started", "params": {"turn": {"id": turn_id, "status": "inProgress"}}})
        cid = (msg.get("params") or {}).get("clientUserMessageId")
        emit({"method": "item/completed", "params": {"item": {
            "type": "userMessage", "id": "um-prompt", "clientId": cid,
            "content": (msg.get("params") or {}).get("input")}}})
        emit({"id": 9001, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": thread_id, "turnId": turn_id, "itemId": "cmd-1"}})
        pathlib.Path("codex-ready").write_text("ready", encoding="utf-8")
    elif method == "turn/steer" and "id" in msg:
        cid = (msg.get("params") or {}).get("clientUserMessageId")
        emit({"id": msg["id"], "result": {"turnId": turn_id}})
        emit({"method": "item/completed", "params": {"item": {
            "type": "userMessage", "id": "um-steer", "clientId": cid,
            "content": (msg.get("params") or {}).get("input")}}})
        emit({"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "id": "am-1", "text": "commentary only", "phase": "commentary"}}})
        emit({"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "id": "am-2", "text": "FINAL_ANSWER", "phase": "final_answer"}}})
        emit({"method": "turn/completed", "params": {"turn": {"id": turn_id, "status": "completed"}}})
pathlib.Path("codex-stdin.json").write_text(json.dumps(seen), encoding="utf-8")
"""


@unittest.skipUnless(os.name == "posix", "process-group supervise is POSIX")
class CodexSuperviseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.stub = self.root / "stub_codex.py"
        self.stub.write_text(CODEX_STUB, encoding="utf-8")
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

    def test_running_codex_confirms_the_inject_on_the_receipt(self):
        install = self.app.execution.installations["codex-local"]
        install["argv"] = [sys.executable, str(self.stub), "exec", "--sandbox", "workspace-write", "-"]
        install["prompt_mode"] = "stdin"
        sent = self.app.send("bot-main", {"text": "task", "allow_write": True}, "stream")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {
            "prompt": "SENTINEL_PROMPT_DO_NOT_LEAK"})
        task = self.app.store.get("task", submitted["task_id"])
        task["attempt_uuid"] = "attempt-codex"
        self.app.store.put("task", task)
        task = self.app.store.get("task", task["id"])
        host = self.app.execution.hosts[task["host_id"]]
        directory = Path(self.app.execution.directory(host, task["execution_id"]))
        spec = self.app.execution.spec(task)
        self.assertEqual(spec["stdin_protocol"], "codex-app-server")
        self.app.execution.prepare(host, str(directory), spec)
        self.directory = directory

        def work():
            with patch.object(runner, "_detect_supervision", return_value=runner.PGROUP_COOPERATIVE):
                runner.supervise(directory)

        self.thread = threading.Thread(target=work)
        self.thread.start()
        ready = self.workspace / "codex-ready"
        deadline = time.time() + 8
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists())
        message = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        runner.append_inject(directory, message, "加一句")
        self.thread.join(10)
        self.assertFalse(self.thread.is_alive())
        argv = json.loads((self.workspace / "codex-argv.json").read_text(encoding="utf-8"))
        self.assertIn("app-server", argv)
        self.assertIn("--listen=stdio://", argv)
        self.assertNotIn("exec", argv)
        self.assertNotIn("SENTINEL_PROMPT_DO_NOT_LEAK", argv)
        self.assertNotIn("--sandbox", argv)
        self.assertTrue(all("priority" not in part for part in argv))
        self.assertNotIn("fork", argv)
        seen = json.loads((self.workspace / "codex-stdin.json").read_text(encoding="utf-8"))
        methods = [item.get("method") for item in seen]
        self.assertNotIn("thread/fork", methods)
        self.assertNotIn("priority", json.dumps(seen))
        self.assertNotIn("accept", json.dumps(seen))
        self.assertFalse(any(item.get("id") == 9001 for item in seen))
        started = next(item for item in seen if item.get("method") == "thread/start")
        self.assertEqual(started["params"]["sandbox"], "workspace-write")
        self.assertEqual(started["params"]["approvalPolicy"], "never")
        self.assertNotIn("approvalsReviewer", started["params"])
        prompt_turn = next(item for item in seen if item.get("method") == "turn/start")
        self.assertEqual(prompt_turn["params"]["threadId"], "thr_from_server")
        self.assertNotEqual(prompt_turn["params"]["threadId"], str(uuid.UUID(hex=task["execution_id"][5:])))
        self.assertIn("SENTINEL_PROMPT_DO_NOT_LEAK", prompt_turn["params"]["input"][0]["text"])
        self.assertEqual(set(prompt_turn["params"]), {"threadId", "input", "clientUserMessageId"})
        steer = next(item for item in seen if item.get("method") == "turn/steer")
        self.assertEqual(steer["params"]["threadId"], "thr_from_server")
        self.assertEqual(steer["params"]["expectedTurnId"], "turn_from_server")
        self.assertEqual(steer["params"]["clientUserMessageId"], message)
        self.assertEqual(steer["params"]["input"], [{"type": "text", "text": "加一句"}])
        self.assertEqual(set(steer["params"]), {
            "threadId", "expectedTurnId", "input", "clientUserMessageId"})
        receipt = runner.inspect(directory, include_tail=True)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["interjection_tier"], 1)
        self.assertEqual(receipt["result_text"], "FINAL_ANSWER")
        self.assertEqual(receipt["tail"], "FINAL_ANSWER")
        self.assertEqual(receipt["injects"], [{"uuid": message, "delivered": True}])


if __name__ == "__main__":
    unittest.main()
