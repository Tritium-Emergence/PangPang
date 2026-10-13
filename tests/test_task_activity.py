import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from test_bot_runtime import call, config
from pangpang.activity import activity_projection, sanitize_rel_path
from pangpang.http import serve
from pangpang.planner_prompt import PLANNER_SYSTEM
from pangpang.runtime import Runtime
from pangpang.service import Service, weakest_route_quality
from pangpang.store import Fault, in_transaction, uid
from pangpang import runner


_TAIL = 16384
_CAP = 4 * 1024 * 1024


def _claude(name, path=None, **extra):
    payload = dict(extra)
    if path is not None:
        payload["file_path"] = path
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": payload}]}}


def _line(obj):
    return json.dumps(obj, ensure_ascii=False) + "\n"


class QueryModel:
    def __init__(self):
        self.tool_names = []
        self.systems = []

    def complete(self, profile, messages, tools):
        if messages[-1].get("role") == "tool":
            return {"content": "已按你的要求处理。"}, {}
        system = messages[0]["content"]
        self.systems.append(system)
        user = next(m["content"] for m in reversed(messages) if m.get("role") == "user")
        rule = "neither an addition nor a correction" in system and "target.activity" in system
        dep_id = "missing"
        try:
            packet = json.loads(system[system.index("{"):])
            for dep in packet.get("dependencies") or []:
                if dep.get("status") == "pending" and dep.get("kind") == "task":
                    dep_id = dep["id"]
                    break
        except (ValueError, KeyError):
            pass
        if not rule:
            self.tool_names.append("send_bot_message")
            return call("send_bot_message", dep_id=dep_id, text="should-not-inject"), {}
        if "停" in user:
            self.tool_names.append("cancel_dependency")
            return call("cancel_dependency", dep_id=dep_id), {}
        if "改成" in user:
            self.tool_names.append("send_bot_message")
            return call("send_bot_message", dep_id=dep_id, text="把标题改成红色"), {}
        return {"content": "任务仍在运行。"}, {}


class TaskActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.app = Service(config(self.root), Path(self.tmp.name) / "data")
        self.app.probe("codex-local")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _task(self, **extra):
        task = {"id": "task-1", "status": "running", "host_id": "local",
                "execution_id": "exec-" + "a" * 32, "cwd": str(self.root),
                "created": 1_000_000, "started": 1_000_050, "required_outputs": []}
        task.update(extra)
        return task

    def _project(self, log, task=None, *, directory=None):
        task = task or self._task()
        folder = Path(directory or (self.root / "run"))
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "output.log"
        path.write_bytes(log if isinstance(log, bytes) else log.encode())
        return activity_projection(task, now=1_000_100, directory=str(folder), local=True,
                                   roots=[str(self.root)], cwd=str(self.root))

    def _bounded(self, activity, *, full=True):
        if not full:
            self.assertEqual(set(activity), {"status", "age_s"})
            return
        self.assertEqual(set(activity), {
            "status", "age_s", "last_log_at", "observation_capped", "recent", "present_outputs"})
        self.assertIn(activity["observation_capped"], {"maybe", "no"})
        self.assertNotEqual(activity["observation_capped"], "yes")
        self.assertLessEqual(len(activity["recent"]), 3)
        self.assertLessEqual(len(activity["present_outputs"]), 10)
        for step in activity["recent"]:
            self.assertTrue(set(step) <= {"tool", "path"})
            self.assertIn(step["tool"], {"read", "write", "edit", "delete", "move", "search", "execute", "fetch", "list"})
        for item in activity["present_outputs"]:
            self.assertEqual(set(item), {"path", "present"})
            self.assertIsInstance(item["present"], bool)

    def _pad_tail(self, tail):
        self.assertLessEqual(len(tail), _TAIL)
        return b"Q" * 80 + b"\n" + tail + b"\n" * (_TAIL - len(tail))

    def _packet(self, turn_id):
        system = Runtime(self.app).context(self.app.store.get("turn", turn_id))[0]["content"]
        return json.loads(system[system.index("{"):])

    def _write_run_log(self, task, text):
        host = self.app.execution.hosts[task["host_id"]]
        folder = Path(self.app.execution.directory(host, task["execution_id"]))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "output.log").write_text(text, encoding="utf-8")
        return folder

    def test_prompt_keeps_direct_answer_rules_and_names_activity(self):
        self.assertIn("neither an addition nor a correction", PLANNER_SYSTEM)
        self.assertIn("dependencies[].target.activity", PLANNER_SYSTEM)
        self.assertNotIn("dependencies[].activity", PLANNER_SYSTEM)
        self.assertIn("When this turn dispatches or continues a Work, your first line states whether you read it as an addition, a correction, or a new matter", PLANNER_SYSTEM)
        self.assertIn("When no work is dispatched, answer directly without any classification or context-state preamble.", PLANNER_SYSTEM)
        self.assertFalse(any("一" <= ch <= "鿿" for ch in PLANNER_SYSTEM))

    def test_age_uses_created_for_queued_and_started_for_launching(self):
        now = 1_000_100
        queued = activity_projection(self._task(status="queued", created=now - 40, started=now - 3), now=now, local=True)
        starting = activity_projection(self._task(status="starting", created=now - 40, started=now - 3), now=now, local=True)
        running = activity_projection(self._task(status="running", created=now - 40, started=now - 3), now=now, local=True)
        self.assertEqual(queued["age_s"], 40)
        self.assertEqual(starting["age_s"], 3)
        self.assertEqual(running["age_s"], 3)
        self.assertEqual(starting["status"], "starting")

    def test_claude_codex_and_acp_fields_are_bounded(self):
        claude = "".join(_line(_claude(name, path, command="echo CLAUDE_COMMAND_SECRET")) for name, path in (
            ("Read", "p1.py"), ("Write", "p2.py"), ("Edit", "p3.py"), ("Bash", None), ("Grep", "p5.py"), ("FooBar", "p6.py")))
        activity = self._project(claude)
        self._bounded(activity)
        self.assertEqual([step["tool"] for step in activity["recent"]], ["edit", "execute", "search"])
        self.assertEqual(activity["recent"][0]["path"], "p3.py")
        self.assertNotIn("path", activity["recent"][1])
        self.assertEqual(activity["recent"][2]["path"], "p5.py")
        blob = json.dumps(activity)
        self.assertNotIn("CLAUDE_COMMAND_SECRET", blob)
        self.assertNotIn("p1.py", blob)
        self.assertNotIn("p6.py", blob)
        self.assertNotIn("FooBar", blob)

        codex = "".join([
            _line({"type": "item.completed", "item": {"type": "command_execution", "command": "echo CODEX_COMMAND_SECRET"}}),
            _line({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "src/codex.py"}]}}),
        ])
        activity = self._project(codex, directory=self.root / "codex")
        self._bounded(activity)
        self.assertEqual(activity["recent"], [{"tool": "execute"}, {"tool": "edit", "path": "src/codex.py"}])
        self.assertNotIn("CODEX_COMMAND_SECRET", json.dumps(activity))

        app_server = "".join([
            _line({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "CODEX_TEXT_SECRET"}}}),
            _line({"method": "item/completed", "params": {"item": {"type": "commandExecution", "command": "echo CODEX_APP_SECRET"}}}),
            _line({"method": "item/started", "params": {"item": {"type": "fileChange", "changes": [{"path": "src/codex-app.py"}]}}}),
        ])
        activity = self._project(app_server, directory=self.root / "codex-app")
        self._bounded(activity)
        self.assertEqual([step["tool"] for step in activity["recent"]], ["execute", "edit"])
        self.assertEqual(activity["recent"][1]["path"], "src/codex-app.py")
        self.assertNotIn("CODEX_TEXT_SECRET", json.dumps(activity))
        self.assertNotIn("CODEX_APP_SECRET", json.dumps(activity))

        acp = "".join([
            _line({"method": "session/update", "params": {"update": {
                "sessionUpdate": "tool_call", "kind": "read", "title": "ACP_TITLE_SECRET",
                "locations": [{"path": "src/acp.py"}]}}}),
            _line({"method": "session/update", "params": {"update": {
                "sessionUpdate": "tool_call_update", "kind": "think", "title": "ACP_THINK_SECRET"}}}),
            _line({"method": "session/update", "params": {"update": {
                "sessionUpdate": "tool_call", "kind": "other", "title": "ACP_OTHER_SECRET"}}}),
            _line({"method": "session/update", "params": {"update": {
                "sessionUpdate": "tool_call", "kind": "execute", "locations": [{"path": "src/run.py"}]}}}),
        ])
        activity = self._project(acp, directory=self.root / "acp")
        self._bounded(activity)
        self.assertEqual(activity["recent"], [
            {"tool": "read", "path": "src/acp.py"}, {"tool": "execute", "path": "src/run.py"}])
        blob = json.dumps(activity)
        self.assertNotIn("ACP_TITLE_SECRET", blob)
        self.assertNotIn("ACP_THINK_SECRET", blob)
        self.assertNotIn("ACP_OTHER_SECRET", blob)

    def test_fragments_are_dropped(self):
        kept = _line(_claude("Read", "kept-plain.py")).encode()
        malformed = self._project(b"{not json MALFORMED_TOKEN\n" + kept, directory=self.root / "malformed")
        self.assertIn("kept-plain.py", json.dumps(malformed))
        self.assertNotIn("MALFORMED_TOKEN", json.dumps(malformed))

        dropped = _line(_claude("Read", "dropped-head.py")).encode()
        kept_head = _line(_claude("Read", "kept-head.py")).encode()
        head = self._project(self._pad_tail(dropped + kept_head), directory=self.root / "head")
        self.assertNotIn("dropped-head.py", json.dumps(head))
        self.assertIn("kept-head.py", json.dumps(head))

        utf = self._project(self._pad_tail(b"\xadUTF8CUT_SECRET\n" + _line(_claude("Read", "kept-utf.py")).encode()),
                            directory=self.root / "utf")
        self.assertNotIn("UTF8CUT_SECRET", json.dumps(utf))
        self.assertIn("kept-utf.py", json.dumps(utf))

        whole = json.dumps(_claude("Read", "stderr-split.py"))
        mid = len(whole) // 2
        split = (whole[:mid] + "\n*** stderr ***\n" + whole[mid:] + "\n").encode()
        split += _line(_claude("Read", "kept-split.py")).encode()
        stderr = self._project(split, directory=self.root / "stderr")
        self.assertNotIn("stderr-split.py", json.dumps(stderr))
        self.assertIn("kept-split.py", json.dumps(stderr))

        trail = (_line(_claude("Read", "kept-trail.py")) + '{"text":"TRAIL_FRAGMENT_SECRET"}').encode()
        trailed = self._project(trail, directory=self.root / "trail")
        self.assertIn("kept-trail.py", json.dumps(trailed))
        self.assertNotIn("TRAIL_FRAGMENT_SECRET", json.dumps(trailed))

    def test_paths_are_sanitized(self):
        outside = self.root.parent / "outside-secret"
        outside.mkdir()
        link = self.root / "escape"
        link.symlink_to(outside, target_is_directory=True)
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        hex_name = "ab" * 16
        ghp = "ghp_abcdefghijklmnopqrstuvwxyz012345"
        akia = "AKIAabcdefghijklmnopqrstuvwxyz012345"
        redacted = self._project("".join([
            _line(_claude("Read", f"notes/{secret}/a.py")),
            _line(_claude("Read", f"notes/{hex_name}/a.py")),
            _line(_claude("Read", f"notes/{ghp}/a.py")),
        ]), directory=self.root / "redacted")
        self.assertEqual([step.get("path") for step in redacted["recent"]], ["<redacted-path>"] * 3)
        akia_only = self._project(_line(_claude("Read", f"notes/{akia}/a.py")), directory=self.root / "akia")
        self.assertEqual(akia_only["recent"], [{"tool": "read", "path": "<redacted-path>"}])
        rejected = self._project("".join([
            _line(_claude("Read", "escape/secret.txt")),
            _line(_claude("Read", "/tmp/abs-secret.txt")),
            _line(_claude("Read", "../dotdot-secret.txt")),
        ]), directory=self.root / "rejected")
        self.assertEqual(rejected["recent"], [{"tool": "read"}, {"tool": "read"}, {"tool": "read"}])
        kept = self._project("".join([
            _line(_claude("Read", "notes/\x01control-secret.py")),
            _line(_claude("Read", "./src/ok.py")),
        ]), directory=self.root / "kept")
        self._bounded(kept)
        self.assertEqual(kept["recent"][-1], {"tool": "read", "path": "src/ok.py"})
        blob = json.dumps({"redacted": redacted, "akia": akia_only, "rejected": rejected, "kept": kept})
        for secret_text in (secret, hex_name, ghp, akia, "escape/secret.txt",
                            "abs-secret.txt", "dotdot-secret.txt", "control-secret"):
            self.assertNotIn(secret_text, blob, secret_text)
        self.assertNotIn("\\u0001", blob)
        self.assertNotIn("..", blob)

    def test_non_injected_secret_in_the_tail_does_not_leak(self):
        secret = "sk-NOTINJECTEDTAILSECRET0123456789"
        log = _line({"type": "assistant", "message": {"content": [
            {"type": "text", "text": secret},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo " + secret}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "src/ok.py"}},
        ]}})
        activity = self._project(log)
        self.assertEqual(activity["recent"], [{"tool": "execute"}, {"tool": "read", "path": "src/ok.py"}])
        self.assertNotIn(secret, json.dumps(activity))

    def test_symlink_inside_cwd_is_rejected(self):
        real = self.root / "real"
        real.mkdir()
        (real / "file.txt").write_text("x", encoding="utf-8")
        (self.root / "alias").symlink_to(real, target_is_directory=True)
        self.assertIsNone(sanitize_rel_path("alias/file.txt", str(self.root)))

        recent = self._project(_line(_claude("Read", "alias/file.txt")), directory=self.root / "in-cwd-link")
        self.assertEqual(recent["recent"], [{"tool": "read"}])
        self.assertNotIn("alias/file.txt", json.dumps(recent["recent"]))

        exec_id = "exec-" + "b" * 32
        Path(runner.execution_output_dir(self.root, exec_id)).mkdir(parents=True)
        task = self._task(execution_id=exec_id, required_outputs=["alias/file.txt"])
        shown = activity_projection(task, now=1_000_100, local=True, roots=[str(self.root)], cwd=str(self.root))
        self.assertEqual(shown["present_outputs"], [{"path": "<redacted-path>", "present": False}])
        self.assertNotIn("alias/file.txt", json.dumps(shown["present_outputs"]))

    def test_symlink_log_is_not_followed(self):
        folder = self.root / "link-log"
        folder.mkdir()
        secret = "SYMLINK_LOG_SECRET"
        target = folder / "other.log"
        target.write_text(_line(_claude("Read", secret + ".py")), encoding="utf-8")
        (folder / "output.log").symlink_to(target)
        activity = activity_projection(self._task(), now=1_000_100, directory=str(folder), local=True,
                                       roots=[str(self.root)], cwd=str(self.root))
        self.assertIsNone(activity["last_log_at"])
        self.assertEqual(activity["recent"], [])
        self.assertNotIn(secret, json.dumps(activity))

    def test_log_cap_is_maybe_not_yes(self):
        def capped(size, *, spec=None, append=False, name="cap"):
            folder = self.root / name
            folder.mkdir()
            log = folder / "output.log"
            log.write_text("{}\n", encoding="utf-8")
            if append:
                with open(log, "ab") as handle:
                    handle.write(b"x" * size)
            else:
                os.truncate(log, size)
            if spec is not None:
                (folder / "spec.json").write_text(json.dumps({"log_bytes": spec}), encoding="utf-8")
            return activity_projection(self._task(), now=1_000_100, directory=str(folder), local=True,
                                       roots=[str(self.root)], cwd=str(self.root))

        self.assertEqual(capped(_CAP, name="one-stream")["observation_capped"], "maybe")
        self.assertEqual(capped(_CAP - 1, name="under-cap")["observation_capped"], "no")
        self.assertEqual(capped(_CAP * 2, name="two-streams")["observation_capped"], "maybe")
        continued = capped(_CAP, append=True, name="continued")
        self.assertEqual(continued["observation_capped"], "maybe")
        self.assertNotEqual(continued["observation_capped"], "yes")
        self.assertEqual(capped(128, spec=128, name="spec-hit")["observation_capped"], "maybe")
        self.assertEqual(capped(127, spec=128, name="spec-under")["observation_capped"], "no")

    def test_present_outputs_match_verifier(self):
        exec_id = "exec-" + "c" * 32
        required = ["there.txt", "missing.txt"]
        out = Path(runner.execution_output_dir(self.root, exec_id))
        out.mkdir(parents=True)
        (out / "there.txt").write_text("ok", encoding="utf-8")
        task = self._task(execution_id=exec_id, required_outputs=required)
        activity = activity_projection(task, now=1_000_100, local=True, roots=[str(self.root)], cwd=str(self.root))
        checked = runner.verify_required_outputs(required, str(out), [str(self.root)])
        self.assertEqual([item["present"] for item in activity["present_outputs"]], [item["present"] for item in checked])
        self.assertEqual([item["path"] for item in activity["present_outputs"]], required)
        self.assertTrue(activity["present_outputs"][0]["present"])
        self.assertFalse(activity["present_outputs"][1]["present"])
        self.assertNotIn("output", json.dumps(activity["present_outputs"]))

    def test_present_output_display_hides_secret_control_and_overlong(self):
        exec_id = "exec-" + "d" * 32
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        control = "bad\x01control-secret.txt"
        overlong = "z" * 161
        out = Path(runner.execution_output_dir(self.root, exec_id))
        out.mkdir(parents=True)
        (out / secret).write_text("x", encoding="utf-8")
        (out / control).write_bytes(b"x")
        (out / overlong).write_text("x", encoding="utf-8")
        task = self._task(execution_id=exec_id, required_outputs=[secret, control, overlong])
        activity = activity_projection(task, now=1_000_100, local=True, roots=[str(self.root)], cwd=str(self.root))
        self.assertEqual(len(activity["present_outputs"]), 3)
        self.assertTrue(all(item["present"] for item in activity["present_outputs"]))
        self.assertTrue(all(item["path"] == "<redacted-path>" for item in activity["present_outputs"]))
        blob = json.dumps(activity)
        self.assertNotIn(secret, blob)
        self.assertNotIn("control-secret", blob)
        self.assertNotIn(overlong, blob)
        self.assertNotIn("\\u0001", blob)

    def test_undeclared_required_outputs_stay_empty(self):
        exec_id = "exec-" + "e" * 32
        out = Path(runner.execution_output_dir(self.root, exec_id))
        out.mkdir(parents=True)
        (out / "made.txt").write_text("x", encoding="utf-8")
        task = self._task(execution_id=exec_id, required_outputs=[], outputs=["made.txt"])
        activity = activity_projection(task, now=1_000_100, local=True, roots=[str(self.root)], cwd=str(self.root))
        self.assertEqual(activity["present_outputs"], [])
        copy = Path(__file__).resolve().parents[1].joinpath("web-src/i18n/zh-CN.ts").read_text(encoding="utf-8")
        self.assertIn("未声明必需文件", copy)
        self.assertNotIn("尚未产出", copy)

    def test_present_outputs_checks_at_most_ten(self):
        exec_id = "exec-" + "f" * 32
        names = [f"f{i:02d}.txt" for i in range(12)]
        out = Path(runner.execution_output_dir(self.root, exec_id))
        out.mkdir(parents=True)
        for name in names:
            (out / name).write_text("x", encoding="utf-8")
        (out / names[3]).write_text("", encoding="utf-8")
        seen = []
        real = runner.verify_required_outputs

        def spy(required, output_dir, roots):
            seen.append(list(required))
            return real(required, output_dir, roots)

        task = self._task(execution_id=exec_id, required_outputs=names)
        with patch("pangpang.activity.runner.verify_required_outputs", spy):
            activity = activity_projection(task, now=1_000_100, local=True, roots=[str(self.root)], cwd=str(self.root))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], names[:10])
        checked = real(names[:10], str(out), [str(self.root)])
        self.assertEqual([item["present"] for item in activity["present_outputs"]], [item["present"] for item in checked])
        self.assertEqual([item["path"] for item in activity["present_outputs"]], names[:10])
        self.assertNotIn(names[10], json.dumps(activity))
        self.assertNotIn(names[11], json.dumps(activity))

    def test_pending_task_activity_does_not_cross(self):
        sent = self.app.send("bot-main", {"text": "两件事", "allow_write": True}, "two")
        first = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "甲", "installation_id": "codex-local"})
        second = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "乙", "installation_id": "codex-local"})
        self._write_run_log(self.app.store.get("task", first["task_id"]), _line(_claude("Read", "a-only.py")))
        self._write_run_log(self.app.store.get("task", second["task_id"]), _line(_claude("Read", "b-only.py")))
        packet = self._packet(sent["turn_id"])
        by_id = {dep["target"]["task_id"]: dep["target"]["activity"]
                 for dep in packet["dependencies"] if dep.get("kind") == "task" and dep.get("status") == "pending"}
        self.assertIn("a-only.py", json.dumps(by_id[first["task_id"]]))
        self.assertNotIn("b-only.py", json.dumps(by_id[first["task_id"]]))
        self.assertIn("b-only.py", json.dumps(by_id[second["task_id"]]))
        self.assertNotIn("a-only.py", json.dumps(by_id[second["task_id"]]))

    def test_gemini_and_unknown_logs_only_set_last_log_at(self):
        for name, text in (
                ("gemini", _line({"type": "content", "text": "GEMINI_FREE_TEXT_SENTINEL"})),
                ("unknown", _line({"foo": "UNKNOWN_FORMAT_TEXT"}))):
            activity = self._project(text, directory=self.root / name)
            self._bounded(activity)
            self.assertEqual(activity["recent"], [])
            self.assertIsNotNone(activity["last_log_at"])
            self.assertNotIn("GEMINI_FREE_TEXT_SENTINEL", json.dumps(activity))
            self.assertNotIn("UNKNOWN_FORMAT_TEXT", json.dumps(activity))

    def test_connector_does_not_read_files(self):
        folder = self.root / "connector-run"
        folder.mkdir()
        (folder / "output.log").write_text(_line(_claude("Read", "connector-leak.py")), encoding="utf-8")
        calls = []
        real = os.lstat

        def spy(path):
            calls.append(os.fspath(path))
            return real(path)

        with patch("pangpang.activity.os.lstat", spy):
            activity = activity_projection(self._task(status="running"), now=1_000_100, directory=str(folder),
                                           local=False, roots=[str(self.root)], cwd=str(self.root))
        self.assertEqual(calls, [])
        self._bounded(activity, full=False)
        self.assertNotIn("connector-leak.py", json.dumps(activity))

        remote = self.root / "remote-runner"
        self.app.execution.hosts["connector-1"] = {
            "id": "connector-1", "transport": "connector", "os": "Linux",
            "runner_root": str(remote), "allowed_roots": [str(self.root)]}
        sent = self.app.send("bot-main", {"text": "远程", "allow_write": True}, "remote")
        res = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "远程任务", "installation_id": "codex-local"})
        task = self.app.store.get("task", res["task_id"])
        task.update(host_id="connector-1", status="running", started=task["created"])
        self.app.store.put("task", task)
        planted = remote / task["execution_id"]
        planted.mkdir(parents=True)
        (planted / "output.log").write_text(_line(_claude("Read", "remote-leak.py")), encoding="utf-8")
        local_runs = Path(self.tmp.name) / "data" / "runs" / task["execution_id"]
        local_runs.mkdir(parents=True)
        (local_runs / "output.log").write_text(_line(_claude("Read", "local-leak.py")), encoding="utf-8")
        calls.clear()
        with patch("pangpang.activity.os.lstat", spy):
            packet = self._packet(sent["turn_id"])
        target = next(dep["target"] for dep in packet["dependencies"] if dep.get("target", {}).get("task_id") == task["id"])
        self._bounded(target["activity"], full=False)
        self.assertFalse(any(task["execution_id"] in path for path in calls))
        blob = json.dumps(target["activity"])
        self.assertNotIn("remote-leak.py", blob)
        self.assertNotIn("local-leak.py", blob)

    def test_packet_and_snapshot_read_files_without_the_store_lock(self):
        sent = self.app.send("bot-main", {"text": "快照", "allow_write": True}, "snap")
        res = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "本地", "installation_id": "codex-local"})
        task = self.app.store.get("task", res["task_id"])
        task.update(status="running", started=task["created"])
        self.app.store.put("task", task)
        self._write_run_log(task, _line(_claude("Read", "src/live.py")))
        self.app.execution.hosts["connector-1"] = {
            "id": "connector-1", "transport": "connector", "os": "Linux",
            "runner_root": str(self.root / "remote-snap"), "allowed_roots": [str(self.root)]}
        remote_id = "exec-" + "9" * 32
        remote_dir = self.root / "remote-snap" / remote_id
        remote_dir.mkdir(parents=True)
        (remote_dir / "output.log").write_text(_line(_claude("Read", "snap-remote-leak.py")), encoding="utf-8")
        self.app.store.put("task", {"id": uid("task"), "status": "running", "host_id": "connector-1",
            "installation_id": "codex-local", "execution_id": remote_id, "cwd": str(self.root),
            "created": 1, "started": 1, "work_id": sent["work_id"], "owner_bot_id": "bot-main",
            "prompt": "远程", "required_outputs": [], "spec_digest": "x"})
        self.app.store.put("task", {"id": uid("task"), "status": "succeeded", "host_id": "local",
            "installation_id": "codex-local", "execution_id": "exec-" + "8" * 32, "cwd": str(self.root),
            "created": 2, "work_id": sent["work_id"], "owner_bot_id": "bot-main", "prompt": "完成",
            "required_outputs": [], "spec_digest": "x"})
        self.app.store.put("task", {"id": uid("task"), "status": "unknown", "host_id": "local",
            "installation_id": "codex-local", "execution_id": "exec-" + "7" * 32, "cwd": str(self.root),
            "created": 3, "work_id": sent["work_id"], "owner_bot_id": "bot-main", "prompt": "未知",
            "required_outputs": [], "spec_digest": "x"})
        for created, status in ((4, "queued"), (5, "starting"), (6, "cancel_requested"), (7, "awaiting_approval")):
            self.app.store.put("task", {"id": "task-snap-" + status, "status": status, "host_id": "local",
                "installation_id": "codex-local", "execution_id": "exec-" + str(created) * 32, "cwd": str(self.root),
                "created": created, "started": created, "work_id": sent["work_id"], "owner_bot_id": "bot-main",
                "prompt": status, "required_outputs": [], "spec_digest": "x"})
        errors, calls = [], []
        real_lstat = os.lstat
        real_tail = runner.read_tail_bounded

        def spy_lstat(path):
            if in_transaction() or self.app.store.lock._is_owned():
                errors.append("lstat")
            calls.append(os.fspath(path))
            return real_lstat(path)

        def spy_tail(path, n=16384):
            if in_transaction() or self.app.store.lock._is_owned():
                errors.append("tail")
            return real_tail(path, n)

        with patch("pangpang.activity.os.lstat", spy_lstat), patch("pangpang.activity.runner.read_tail_bounded", spy_tail):
            packet = self._packet(sent["turn_id"])
            packet_activity = next(dep["target"]["activity"] for dep in packet["dependencies"]
                                   if dep.get("target", {}).get("task_id") == task["id"])
            self.assertIn("age_s", packet_activity)
            self.assertIsInstance(packet_activity["age_s"], int)
            self.assertTrue(any(dep.get("target", {}).get("activity", {}).get("recent") for dep in packet["dependencies"]))
            token = "s" * 32
            server = serve(self.app, token, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                import http.cookiejar
                import urllib.request
                origin = "http://127.0.0.1:" + str(server.server_port)
                client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

                def request(path, body=None, key="k"):
                    headers = {"Origin": origin}
                    data = None
                    if body is not None:
                        headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
                        data = json.dumps(body).encode()
                    return client.open(urllib.request.Request(origin + "/api/v1/" + path, data=data, headers=headers))

                with request("login", {"token": token}, key="login"):
                    pass
                with request("snapshot") as response:
                    snapshot = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        self.assertEqual(errors, [])
        local = next(row for row in snapshot["tasks"] if row["id"] == task["id"])
        self.assertIn("activity", local)
        self.assertNotIn("age_s", local["activity"])
        self.assertIn("src/live.py", json.dumps(local["activity"]))
        succeeded = next(row for row in snapshot["tasks"] if row["status"] == "succeeded")
        unknown = next(row for row in snapshot["tasks"] if row["status"] == "unknown")
        self.assertNotIn("activity", succeeded)
        self.assertNotIn("activity", unknown)
        for status in ("queued", "starting", "cancel_requested"):
            row = next(item for item in snapshot["tasks"] if item["id"] == "task-snap-" + status)
            self.assertEqual(row["activity"]["status"], status)
            self.assertNotIn("age_s", row["activity"])
        self.assertNotIn("activity", next(item for item in snapshot["tasks"] if item["id"] == "task-snap-awaiting_approval"))
        remote = next(row for row in snapshot["tasks"] if row["host_id"] == "connector-1")
        self.assertNotIn("activity", remote)
        self.assertNotIn("snap-remote-leak.py", json.dumps(snapshot["tasks"]))
        self.assertFalse(any(remote_id in path for path in calls))

    def test_read_work_has_no_activity_and_reads_no_files(self):
        sent = self.app.send("bot-main", {"text": "读取", "allow_write": True}, "read-work")
        res = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "文件", "installation_id": "codex-local"})
        self._write_run_log(self.app.store.get("task", res["task_id"]), _line(_claude("Read", "read-work-leak.py")))

        def fail(*args, **kwargs):
            raise AssertionError("read_work read a file")

        with patch("pangpang.activity.activity_projection", fail), patch("pangpang.runner.read_tail_bounded", fail):
            result = Runtime(self.app).invoke(self.app.store.get("turn", sent["turn_id"]), "read_work", {"work_id": sent["work_id"]})
        for dep in result["dependencies"]:
            self.assertNotIn("activity", dep.get("target") or {})
        self.assertNotIn("read-work-leak.py", json.dumps(result))

    def test_file_read_inside_a_transaction_is_refused(self):
        sent = self.app.send("bot-main", {"text": "事务", "allow_write": True}, "txn")
        res = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "事务", "installation_id": "codex-local"})
        self._write_run_log(self.app.store.get("task", res["task_id"]), _line(_claude("Read", "src/a.py")))
        with self.assertRaises(Fault) as caught:
            with self.app.store.atomic():
                Runtime(self.app).context(self.app.store.get("turn", sent["turn_id"]))
        self.assertIn("io_inside_transaction", caught.exception.code)

    def _finish_seed_turn(self, key):
        sent = self.app.send("bot-main", {"text": "请处理", "allow_write": True}, key)
        res = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "生成文件", "installation_id": "codex-local"})
        turn = self.app.store.get("turn", sent["turn_id"])
        turn["status"] = "done"
        self.app.store.put("turn", turn)
        return sent, res

    def _run_query(self, work_id, text, key):
        model = QueryModel()
        sent = self.app.send("bot-main", {"text": text}, key, routing={
            "work_id": work_id, "route_quality": "deterministic", "route_source": "deterministic"})
        Runtime(self.app, model).run(self.app.store.get("turn", sent["turn_id"]))
        return model, self.app.store.get("turn", sent["turn_id"])

    def test_status_question_calls_no_tools(self):
        sent, _res = self._finish_seed_turn("query")
        model, turn = self._run_query(sent["work_id"], "情况如何？", "query-2")
        self.assertEqual(model.tool_names, [])
        self.assertFalse(any(message.get("tool_calls") for message in turn["history"]))
        self.assertIn("target.activity", model.systems[0])
        reply = next(message for message in self.app.store.find("message", work_id=sent["work_id"])
                     if message.get("kind") == "assistant" and message.get("turn_id") == turn["id"])
        first = reply["content"].splitlines()[0]
        for prefix in ("Addition", "Correction", "new matter", "New matter", "追加", "纠正", "新事"):
            self.assertFalse(first.startswith(prefix), first)

    def test_compound_stop_only_cancels(self):
        sent, _res = self._finish_seed_turn("stop")
        model, turn = self._run_query(sent["work_id"], "停掉它，现在什么进度？", "stop-2")
        names = [call["function"]["name"] for message in turn["history"] if message.get("role") == "assistant"
                 for call in message.get("tool_calls") or []]
        self.assertEqual(names, ["cancel_dependency"])
        self.assertEqual(model.tool_names, ["cancel_dependency"])

    def test_compound_edit_only_sends(self):
        sent, _res = self._finish_seed_turn("edit")
        model, turn = self._run_query(sent["work_id"], "把标题改成红色，另外情况如何？", "edit-2")
        names = [call["function"]["name"] for message in turn["history"] if message.get("role") == "assistant"
                 for call in message.get("tool_calls") or []]
        self.assertEqual(names, ["send_bot_message"])
        self.assertEqual(model.tool_names, ["send_bot_message"])

    def _queued_task(self, key):
        sent = self.app.send("bot-main", {"text": key, "allow_write": True}, key)
        res = self.app.submit_task("bot-main", sent["work_id"], {"prompt": key, "installation_id": "codex-local"})
        return sent, res

    def test_cancel_queued_task(self):
        _sent, res = self._queued_task("cancel-queued")
        done = self.app.cancel(res["task_id"])
        self.assertEqual(done["status"], "cancelled")

    def test_cancel_starting_and_running_requests_cancel(self):
        for status in ("starting", "running"):
            with self.subTest(status=status):
                _sent, res = self._queued_task("cancel-" + status)
                task = self.app.store.get("task", res["task_id"])
                task.update(status=status, started=task["created"])
                self.app.store.put("task", task)
                with patch.object(self.app.execution, "cancel", return_value=None):
                    done = self.app.cancel(task["id"])
                self.assertEqual(done["status"], "cancel_requested")
                self.assertTrue(done.get("device_cancel_sent"))

    def test_late_start_ack_does_not_resume_running(self):
        _sent, res = self._queued_task("late-ack")
        task = self.app.store.get("task", res["task_id"])
        task.update(status="cancel_requested", cancel_requested=True, started=task["created"])
        self.app.store.put("task", task)
        with patch.object(self.app.execution, "start", return_value={"pid": 1}) as started, \
                patch.object(self.app.execution, "cancel", return_value=None):
            self.app._try_start(self.app.store.get("task", task["id"]))
        self.assertTrue(started.called)
        done = self.app.store.get("task", task["id"])
        self.assertEqual(done["status"], "cancel_requested")

    def test_cancel_ack_is_not_terminal(self):
        _sent, res = self._queued_task("ack")
        task = self.app.store.get("task", res["task_id"])
        task.update(status="running", started=task["created"])
        self.app.store.put("task", task)
        with patch.object(self.app.execution, "cancel", return_value=None):
            done = self.app.cancel(task["id"])
        self.assertEqual(done["status"], "cancel_requested")
        self.assertTrue(done.get("device_cancel_sent"))
        self.assertNotIn(done["status"], {"succeeded", "failed", "cancelled"})

    def test_cancel_send_failure_stays_cancel_requested(self):
        _sent, res = self._queued_task("send-fail")
        task = self.app.store.get("task", res["task_id"])
        task.update(status="running", started=task["created"])
        self.app.store.put("task", task)
        with patch.object(self.app.execution, "cancel", side_effect=Fault("cancel_transport")):
            done = self.app.cancel(task["id"])
        self.assertEqual(done["status"], "cancel_requested")
        self.assertFalse(done.get("device_cancel_sent"))
        self.assertGreaterEqual(done.get("observation_failures", 0), 1)
        self.assertNotEqual(done["status"], "unknown")

    def test_cancel_request_yields_to_succeeded_receipt(self):
        sent = self.app.send("bot-main", {"text": "合同", "allow_write": True}, "race")
        res = self.app.submit_task("bot-main", sent["work_id"], {
            "prompt": "产出", "installation_id": "codex-local", "required_outputs": ["out.txt"]})
        task = self.app.store.get("task", res["task_id"])
        task.update(status="cancel_requested", cancel_requested=True, started=task["created"], attempt_uuid="attempt-1")
        self.app.store.put("task", task)
        task = self.app.store.get("task", task["id"])
        receipt = {"execution_id": task["execution_id"], "spec_digest": task["spec_digest"],
                   "attempt_uuid": "attempt-1", "status": "succeeded", "process_tree_confirmed": True,
                   "supervision": "windows-job-object", "artifacts": [],
                   "required_output_results": [{"output": "out.txt", "present": True}]}
        self.app.complete_task(task, receipt)
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "succeeded")

    def test_cancel_task_does_not_cancel_parent_work(self):
        sent, res = self._queued_task("parent")
        turn = self.app.store.get("turn", sent["turn_id"])
        turn["status"] = "running"
        self.app.store.put("turn", turn)
        self.app.cancel(res["task_id"])
        self.assertNotEqual(self.app.store.get("work", sent["work_id"])["status"], "cancelled")
        turn = self.app.store.get("turn", sent["turn_id"])
        self.assertEqual(turn["status"], "running")
        self.assertFalse(turn.get("cancel_requested"))

    def test_cancel_work_stops_turns_and_propagates(self):
        sent, queued = self._queued_task("stop-work")
        running_res = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "运行", "installation_id": "codex-local"})
        running_task = self.app.store.get("task", running_res["task_id"])
        running_task.update(status="running", started=running_task["created"])
        self.app.store.put("task", running_task)
        running_turn = self.app.store.put("turn", {
            "id": uid("turn"), "bot_id": "bot-main", "work_id": sent["work_id"],
            "message_id": sent["message_id"], "message_ids": [sent["message_id"]],
            "status": "running", "history": [], "created": 1, "rounds": 0, "priority": 2})
        child_bot = self.app.create_bot({"name": "子", "role": "r"})
        child = self.app.delegate("bot-main", sent["work_id"], {
            "target_bot_id": child_bot["id"], "goal": "g", "request": "r", "acceptance": ["a"]})
        self.app.cancel_work(sent["work_id"])
        self.assertEqual(self.app.store.get("work", sent["work_id"])["status"], "cancelled")
        self.assertEqual(self.app.store.get("turn", sent["turn_id"])["status"], "cancelled")
        stopped = self.app.store.get("turn", running_turn["id"])
        self.assertEqual(stopped["status"], "running")
        self.assertTrue(stopped.get("cancel_requested"))
        self.assertEqual(self.app.store.get("task", queued["task_id"])["status"], "cancelled")
        self.assertEqual(self.app.store.get("task", running_res["task_id"])["status"], "cancel_requested")
        deps = self.app.store.find("dependency", source_work_id=sent["work_id"])
        self.assertTrue(deps)
        self.assertTrue(all(dep["status"] == "settled" for dep in deps))
        self.assertEqual(self.app.store.get("work", child["work_id"])["status"], "cancelled")

    def test_queued_merge_keeps_weakest_route_quality(self):
        self.assertEqual(weakest_route_quality("fallback", "deterministic"), "fallback")
        self.assertEqual(weakest_route_quality("deterministic", "fallback"), "fallback")
        self.assertEqual(weakest_route_quality("unexpected", "fallback"), "fallback")
        self.assertEqual(weakest_route_quality("unexpected", "deterministic"), "fallback")
        first = self.app.send("bot-main", {"text": "先"}, "weak-a", routing={
            "route_quality": "fallback", "route_source": "fallback"})
        second = self.app.send("bot-main", {"text": "后"}, "weak-b", routing={
            "work_id": first["work_id"], "route_quality": "deterministic", "route_source": "deterministic"})
        self.assertEqual(first["turn_id"], second["turn_id"])
        self.assertEqual(self.app.store.get("turn", first["turn_id"])["route_quality"], "fallback")
        earlier = self.app.send("bot-main", {"text": "先"}, "weak-c", routing={
            "route_quality": "deterministic", "route_source": "deterministic"})
        later = self.app.send("bot-main", {"text": "后"}, "weak-d", routing={
            "work_id": earlier["work_id"], "route_quality": "fallback", "route_source": "fallback"})
        self.assertEqual(earlier["turn_id"], later["turn_id"])
        self.assertEqual(self.app.store.get("turn", earlier["turn_id"])["route_quality"], "fallback")


class ActivityCardTests(unittest.TestCase):
    def test_card_copy(self):
        env = os.environ.copy()
        env.pop("TYPESAFE_API_KEY", None)
        proc = subprocess.run(
            ["node", "--experimental-strip-types", "--test", "tests/test_activity_card.mjs"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)


if __name__ == "__main__":
    unittest.main()
