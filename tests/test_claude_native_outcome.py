"""Repair #78 P0-1: the Claude stream-json native result outcome.

A `type=result` frame can report `is_error=true` / an error subtype and the CLI
can still exit 0. The receipt gains the same optional `native_outcome` shape as
the Codex slice (status + one fixed short code + the frame-local session id),
and the real `exit_code` is preserved. Higher-priority facts (control-plane
cancel, timeout, non-zero exit, unconfirmed cleanup) keep their precedence.

The fold tests drive `apply_stream_event` directly. The supervise tests run a
local Claude stream-json stub through the real runner and the existing Service
settlement path. No real Claude, Codex, Jev, or external service is used.
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
from pangpang.semantic_gate import SemanticGate
from pangpang.service import Service
from pangpang.store import Fault
from pangpang.task_gate import TaskResultGate


ERROR = "claude_result_error"
SENTINEL = "SENTINEL_PROVIDER_TEXT_DO_NOT_LEAK"
PROMPT_SENTINEL = "SENTINEL_PROMPT_DO_NOT_LEAK"
SESSION = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _state():
    return {"result_text": None, "confirmed": set()}


class ClaudeNativeOutcomeFoldTests(unittest.TestCase):
    def _assert_failed(self, event, *, session_id=None):
        state = _state()
        runner.apply_stream_event(state, dict(event))
        self.assertEqual(state["native_outcome"], {
            "status": "failed", "error": ERROR, "session_id": session_id,
        })
        self.assertEqual(state["result_seq"], 1)

    def test_error_signals_are_exact_and_bounded(self):
        # The boolean identity alone is a failure, and it wins over success.
        self._assert_failed({"type": "result", "is_error": True})
        self._assert_failed({"type": "result", "is_error": True,
                             "subtype": "success", "session_id": SESSION},
                            session_id=SESSION)
        # Each recognized subtype fails alone, with the flag absent or false.
        for subtype in ("error_during_execution", "error_max_turns",
                        "error_max_budget_usd",
                        "error_max_structured_output_retries"):
            for extra in ({}, {"is_error": False}):
                with self.subTest(subtype=subtype, extra=extra):
                    self._assert_failed(dict(
                        {"type": "result", "subtype": subtype}, **extra))
        # A malformed subtype next to a true flag still fails, never raises.
        for subtype in (["error_during_execution"], {"a": 1}, None, 1, True):
            with self.subTest(subtype=subtype):
                self._assert_failed({"type": "result", "is_error": True,
                                     "subtype": subtype})
        # The code is fixed ASCII, <= 64, and never copies provider text.
        state = _state()
        runner.apply_stream_event(state, {
            "type": "result", "is_error": True,
            "subtype": "error_during_execution",
            "error": SENTINEL + " " + ("X" * 5000),
            "errors": [SENTINEL],
            "result": SENTINEL + "-RESULT",
            "session_id": SESSION,
        })
        outcome = state["native_outcome"]
        self.assertEqual(outcome["error"], ERROR)
        self.assertTrue(ERROR.isascii())
        self.assertLessEqual(len(ERROR), 64)
        self.assertNotIn(SENTINEL, json.dumps(outcome))

    def test_success_unknown_and_malformed_results_preserve_compatibility(self):
        for extra in ({}, {"is_error": False}, {"is_error": None},
                      {"is_error": 1}, {"is_error": "true"},
                      {"is_error": {"ok": True}}):
            # A non-boolean is_error does not manufacture failure; subtype
            # success still reports completed. Only boolean true fails.
            with self.subTest(extra=extra):
                state = _state()
                runner.apply_stream_event(state, dict(
                    {"type": "result", "subtype": "success",
                     "session_id": SESSION}, **extra))
                self.assertEqual(state["native_outcome"], {
                    "status": "completed", "error": None,
                    "session_id": SESSION,
                })
        # Unknown/absent/malformed subtype and non-boolean is_error truthiness
        # do not invent a native fact and do not raise.
        events = [
            {"type": "result"},
            {"type": "result", "subtype": "weird", "is_error": False},
            {"type": "result", "subtype": "error_future_kind", "is_error": False},
            {"type": "result", "subtype": "error_during_execution_extra",
             "is_error": False},
            {"type": "result", "subtype": "SUCCESS", "is_error": False},
            {"type": "result", "subtype": ["success"], "is_error": False},
            {"type": "result", "subtype": {"a": 1}, "is_error": False},
            {"type": "result", "subtype": None, "is_error": False},
            {"type": "result", "is_error": 1},
            {"type": "result", "is_error": "true"},
            {"type": "result", "is_error": {"ok": True}},
        ]
        for event in events:
            with self.subTest(event=event):
                state = _state()
                runner.apply_stream_event(state, dict(event))
                self.assertIsNone(state.get("native_outcome"))
                self.assertEqual(state["result_seq"], 1)
        # A legacy result without a signal still updates text and delivery.
        state = _state()
        runner.apply_stream_event(state, {
            "type": "result", "result": "legacy", "user_message_uuid": "u1"})
        self.assertIsNone(state.get("native_outcome"))
        self.assertEqual(state["result_text"], "legacy")
        self.assertEqual(state["confirmed"], {"u1"})

    def test_later_result_replaces_outcome_in_both_directions(self):
        state = _state()
        runner.apply_stream_event(state, {"type": "result",
                                          "subtype": "success", "is_error": False,
                                          "result": "OLD_FINAL"})
        self.assertEqual(state["native_outcome"]["status"], "completed")
        runner.apply_stream_event(state, {"type": "result",
                                          "subtype": "error_max_turns",
                                          "is_error": False})
        self.assertEqual(state["native_outcome"], {
            "status": "failed", "error": ERROR, "session_id": None})
        # The old final is retained as diagnostics, never consulted for status.
        self.assertEqual(state["result_text"], "OLD_FINAL")
        runner.apply_stream_event(state, {"type": "result",
                                          "subtype": "success", "is_error": False})
        self.assertEqual(state["native_outcome"], {
            "status": "completed", "error": None, "session_id": None})
        runner.apply_stream_event(state, {"type": "result", "is_error": True})
        self.assertEqual(state["native_outcome"]["status"], "failed")
        # An unrecognized result clears the previous failure.
        runner.apply_stream_event(state, {"type": "result", "is_error": False})
        self.assertIsNone(state["native_outcome"])
        self.assertEqual(state["result_text"], "OLD_FINAL")

        # Non-result events never clear a known outcome.
        state = _state()
        runner.apply_stream_event(state, {"type": "result", "is_error": True})
        for event in (
                {"type": "assistant", "message": {"content": "chatter"}},
                {"type": "system", "subtype": "init"},
                {"type": "user", "uuid": "replay", "isReplay": True},
                {"type": "control_request",
                 "request": {"subtype": "can_use_tool"}}):
            with self.subTest(event=event["type"]):
                runner.apply_stream_event(state, event)
                self.assertEqual(state["native_outcome"]["status"], "failed")

    def test_session_identity_is_optional_and_result_local(self):
        cases = [
            ({"session_id": SESSION}, SESSION),
            ({"session_id": None}, None),
            ({"session_id": ""}, None),
            ({"session_id": 123}, None),
            ({"session_id": ["x"]}, None),
            ({"session_id": {"id": SESSION}}, None),
            ({}, None),
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                state = _state()
                runner.apply_stream_event(state, dict(
                    {"type": "result", "is_error": True}, **extra))
                outcome = state["native_outcome"]
                self.assertEqual(outcome["session_id"], expected)
                self.assertEqual(outcome["status"], "failed")
                self.assertNotIn("thread_id", outcome)
                self.assertNotIn("turn_id", outcome)
        # A later result replaces the identity; it never inherits the old one.
        state = _state()
        runner.apply_stream_event(state, {"type": "result", "is_error": True,
                                          "session_id": SESSION})
        runner.apply_stream_event(state, {"type": "result", "subtype": "success",
                                          "is_error": False})
        self.assertEqual(state["native_outcome"], {
            "status": "completed", "error": None, "session_id": None})
        # Missing identity does not mask a failure.
        state = _state()
        runner.apply_stream_event(state, {"type": "result", "is_error": True})
        self.assertEqual(state["native_outcome"]["status"], "failed")

    def test_result_sequence_text_and_delivery_are_unchanged(self):
        state = _state()
        runner.apply_stream_event(state, {
            "type": "result", "subtype": "success", "result": "one",
            "user_message_uuids": ["u1", "u2"]})
        runner.apply_stream_event(state, {
            "type": "result", "subtype": "success", "result": "two",
            "user_message_uuids": ["u2"]})
        # A duplicate result still advances the counter; the last text wins.
        self.assertEqual(state["result_seq"], 2)
        self.assertEqual(state["result_text"], "two")
        self.assertEqual(state["confirmed"], {"u1", "u2"})
        # A repeated result is content-idempotent but still advances.
        runner.apply_stream_event(state, {
            "type": "result", "subtype": "success", "result": "two",
            "user_message_uuids": ["u2"]})
        self.assertEqual(state["result_seq"], 3)
        # The list wins when present; the older single field is the fallback.
        state = _state()
        runner.apply_stream_event(state, {
            "type": "result", "user_message_uuid": "last",
            "user_message_uuids": ["first", "last"]})
        self.assertEqual(state["confirmed"], {"first", "last"})
        state = _state()
        runner.apply_stream_event(state, {"type": "result",
                                          "user_message_uuid": "only"})
        self.assertEqual(state["confirmed"], {"only"})
        # A replay echo is not delivery; a permission request is not approved.
        state = _state()
        runner.apply_stream_event(state, {"type": "user", "uuid": "replay",
                                          "isReplay": True})
        runner.apply_stream_event(state, {
            "type": "control_request",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash"}})
        self.assertEqual(state["confirmed"], set())
        self.assertIsNone(state["result_text"])
        self.assertNotIn("permission_decisions", state)


CLAUDE_SCENARIO_STUB = r"""
import json, os, pathlib, sys, time
base = pathlib.Path(__file__).resolve().parent
scenario = json.loads((base / "scenario.json").read_text(encoding="utf-8"))
marker = scenario.get("marker", "stage")
batches = scenario.get("batches") or []

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def mark(name, data=""):
    (base / (marker + "-" + name)).write_text(data, encoding="utf-8")

if scenario.get("no_result"):
    # No result means no idle-close trigger: exit on our own.
    time.sleep(0.1)
    sys.exit(int(scenario.get("exit", 0)))

seen = []
index = 0
while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        frame = json.loads(line)
    except ValueError:
        continue
    seen.append(frame)
    batch = batches[index] if index < len(batches) else []
    index += 1
    for item in batch:
        obj = dict(item)
        if obj.pop("_echo_uuid", False) and frame.get("uuid"):
            obj["user_message_uuids"] = [frame["uuid"]]
        if obj.pop("_echo_session", False):
            obj["session_id"] = frame.get("session_id")
        artifact = obj.pop("_artifact", None)
        if artifact:
            out = os.environ.get("XPP_OUTPUT_DIR")
            if out:
                pathlib.Path(out, artifact).write_text("artifact", encoding="utf-8")
        emit(obj)
    mark("frame-%d" % index, json.dumps({"uuid": frame.get("uuid")}))
    if scenario.get("sleep") is not None:
        time.sleep(float(scenario["sleep"]))
mark("stdin-eof", json.dumps(seen))
sys.exit(int(scenario.get("exit", 0)))
"""


class _ServiceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.stub = self.root / "stub_claude.py"
        self.stub.write_text(CLAUDE_SCENARIO_STUB, encoding="utf-8")
        cfg = config(self.workspace)
        cfg["installations"][0].update({
            "adapter": "claude-code",
            "argv": [sys.executable, str(self.stub)],
            "prompt_mode": "stdin",
        })
        self.cfg = cfg
        self.app = Service(cfg, self.root / "data")
        self.app.store.put("probe", {
            "id": "codex-local", "available": True,
            "supervision": runner.PGROUP_COOPERATIVE})
        self.thread = None
        self.directory = None
        self._key = 0

    def tearDown(self):
        if self.directory is not None:
            started = runner.read_json_bounded(self.directory / "started.json") or {}
            pid = started.get("pid")
            if self.thread is not None and self.thread.is_alive():
                if isinstance(pid, int) and pid > 0:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                self.thread.join(5)
        self.app.store.close()
        self.tmp.cleanup()

    def scenario(self, batches, *, marker=None, no_result=False,
                 exit_code=None, sleep=None):
        marker = marker or ("stage-" + uuid.uuid4().hex[:8])
        data = {"batches": [list(batch) for batch in batches], "marker": marker}
        if no_result:
            data["no_result"] = True
        if exit_code is not None:
            data["exit"] = exit_code
        if sleep is not None:
            data["sleep"] = sleep
        (self.root / "scenario.json").write_text(json.dumps(data), encoding="utf-8")
        return marker

    def marker_path(self, marker, name):
        return self.root / (marker + "-" + name)

    def await_marker(self, marker, name, timeout=10):
        path = self.marker_path(marker, name)
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists(), "stub did not reach " + name)

    def prepare_task(self, prompt="task", *, required_outputs=None, timeout_s=None):
        self._key += 1
        sent = self.app.send("bot-main", {"text": "task", "allow_write": True},
                             "native-%d-%s" % (self._key, uuid.uuid4().hex[:8]))
        body = {"prompt": prompt}
        if required_outputs:
            body["required_outputs"] = required_outputs
        submitted = self.app.submit_task("bot-main", sent["work_id"], body)
        task = self.app.store.get("task", submitted["task_id"])
        task["attempt_uuid"] = "attempt-" + uuid.uuid4().hex
        self.app.store.put("task", task)
        task = self.app.store.get("task", task["id"])
        host = self.app.execution.hosts[task["host_id"]]
        directory = Path(self.app.execution.directory(host, task["execution_id"]))
        spec = self.app.execution.spec(task)
        if timeout_s is not None:
            spec["timeout_s"] = timeout_s
        self.app.execution.prepare(host, str(directory), spec)
        self.directory = directory
        return task, submitted, directory

    def run_supervise(self, directory, *, idle=None):
        def work():
            context = [patch.object(runner, "_detect_supervision",
                                    return_value=runner.PGROUP_COOPERATIVE)]
            if idle is not None:
                context.append(patch.object(runner, "STREAM_IDLE_CLOSE_S", idle))
            try:
                for item in context:
                    item.start()
                runner.supervise(directory)
            finally:
                for item in reversed(context):
                    item.stop()
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def join(self, timeout=15):
        self.thread.join(timeout)
        self.assertFalse(self.thread.is_alive(), "runner did not finish")

    def settle(self, task, directory):
        receipt = runner.inspect(directory, include_tail=True)
        self.app.complete_task(task, receipt)
        return receipt

    def stdin_messages(self, marker):
        path = self.marker_path(marker, "stdin-eof")
        return json.loads(path.read_text(encoding="utf-8"))


@unittest.skipUnless(os.name == "posix", "process-group supervise is POSIX")
class _SuperviseBase(_ServiceBase):
    pass


class ClaudeNativeOutcomeSuperviseTests(_SuperviseBase):
    def _session(self, task):
        return runner.session_id_from_execution(task["execution_id"])

    def _assert_failed_receipt(self, receipt, task):
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["error"], ERROR)
        self.assertEqual(receipt["native_outcome"]["status"], "failed")
        self.assertEqual(receipt["native_outcome"]["error"], ERROR)
        self.assertEqual(receipt["native_outcome"]["session_id"], self._session(task))
        self.assertNotIn("thread_id", receipt["native_outcome"])
        self.assertNotIn("turn_id", receipt["native_outcome"])
        self.assertFalse(receipt["timed_out"])
        self.assertTrue(receipt["process_tree_confirmed"])

    def _assert_failed_settlement(self, task, sent, old_text):
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "failed")
        self.assertEqual(settled["receipt"]["status"], "failed")
        self.assertEqual(settled["receipt"]["exit_code"], 0)
        self.assertEqual(settled["receipt"]["native_outcome"]["status"], "failed")
        dep = self.app.store.get("dependency", sent["dependency_id"])
        self.assertEqual(dep["outcome"], "failed")
        self.assertTrue(dep["summary"].startswith("failed"))
        self.assertIn(ERROR, dep["summary"])
        payload = dep.get("result_payload", {})
        self.assertEqual(payload.get("status"), "failed")
        self.assertEqual(payload.get("error"), ERROR)
        self.assertNotIn("result_text", payload)
        self.assertNotIn(old_text, json.dumps(payload))
        work = self.app.store.get("work", task["work_id"])
        self.assertNotEqual(work["status"], "delivered")
        result_messages = [m for m in self.app.store.find("message", work_id=task["work_id"])
                           if m.get("kind") == "execution_result"]
        self.assertTrue(result_messages)
        self.assertNotIn(old_text, result_messages[-1].get("content", ""))
        # A failed run's already-produced artifacts stay inspectable.
        self.assertTrue(settled.get("artifact_refs"))
        for ref in settled["artifact_refs"]:
            self.assertTrue(self.app.store.get("artifact", ref))

    def test_failed_exit_zero_settles_failed_without_old_final(self):
        # A success result writes a final answer, then the same execution reports
        # an error result without text. The process still exits 0.
        marker = self.scenario([[
            {"type": "result", "subtype": "success", "is_error": False,
             "result": "EARLIER_FINAL",
             "_echo_uuid": True, "_echo_session": True, "_artifact": "report.md"},
            {"type": "result", "subtype": "error_during_execution",
             "is_error": False, "_echo_session": True},
        ]])
        task, sent, directory = self.prepare_task(prompt=PROMPT_SENTINEL)
        self.run_supervise(directory, idle=1.0)
        self.join()
        receipt = self.settle(task, directory)
        self._assert_failed_receipt(receipt, task)
        self.assertEqual(receipt["result_text"], "EARLIER_FINAL")
        self._assert_failed_settlement(task, sent, "EARLIER_FINAL")
        # Re-submitting the same receipt is inert.
        messages = len(self.app.store.find("message", work_id=task["work_id"]))
        artifacts = len(self.app.store.find("artifact"))
        stored = self.app.store.get("task", task["id"])
        self.app.complete_task(stored, receipt)
        self.assertEqual(messages, len(self.app.store.find("message", work_id=task["work_id"])))
        self.assertEqual(artifacts, len(self.app.store.find("artifact")))
        # The failure itself started nothing: one prompt, no interrupt, no retry.
        frames = self.stdin_messages(marker)
        self.assertEqual(len(frames), 1)
        self.assertFalse(any(frame.get("type") == "control_request" for frame in frames))

    def test_boolean_only_and_subtype_only_errors_settle_failed(self):
        cases = {
            "boolean_only": {"type": "result", "is_error": True},
            "subtype_only": {"type": "result", "subtype": "error_max_turns",
                             "is_error": False},
        }
        for name, item in cases.items():
            with self.subTest(name=name):
                self.scenario([[dict(item, _echo_uuid=True, _echo_session=True)]])
                task, sent, directory = self.prepare_task()
                self.run_supervise(directory)
                self.join()
                receipt = self.settle(task, directory)
                self._assert_failed_receipt(receipt, task)
                settled = self.app.store.get("task", task["id"])
                self.assertEqual(settled["status"], "failed")
                dep = self.app.store.get("dependency", sent["dependency_id"])
                self.assertEqual(dep["outcome"], "failed")
                self.assertIn(ERROR, dep["summary"])
                self.assertNotIn("result_text", dep.get("result_payload", {}))

    def test_later_success_clears_failure_and_settles_success(self):
        marker = self.scenario([
            [{"type": "result", "is_error": True, "result": "FAILED_ATTEMPT",
              "_echo_uuid": True, "_echo_session": True}],
            [{"type": "result", "subtype": "success", "is_error": False,
              "result": "FINAL_AFTER_INJECT",
              "_echo_uuid": True, "_echo_session": True}],
        ])
        task, sent, directory = self.prepare_task()
        self.run_supervise(directory, idle=2.0)
        self.await_marker(marker, "frame-1")
        inject = str(uuid.uuid4())
        runner.append_inject(directory, inject, "加一句")
        self.join()
        receipt = self.settle(task, directory)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertNotIn("error", receipt)
        self.assertEqual(receipt["native_outcome"], {
            "status": "completed", "error": None,
            "session_id": self._session(task)})
        self.assertEqual(receipt["result_text"], "FINAL_AFTER_INJECT")
        self.assertEqual(receipt["injects"], [{"uuid": inject, "delivered": True}])
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "succeeded")
        dep = self.app.store.get("dependency", sent["dependency_id"])
        self.assertEqual(dep["outcome"], "success")
        payload = dep.get("result_payload", {})
        self.assertIn("FINAL_AFTER_INJECT", payload.get("result_text", ""))
        self.assertNotIn(ERROR, json.dumps(payload))
        frames = self.stdin_messages(marker)
        self.assertEqual(len(frames), 2)

    def test_success_and_legacy_results_preserve_output_adjudication(self):
        # Positive control: success + exit 0 is still a successful execution.
        self.scenario([[
            {"type": "result", "subtype": "success", "is_error": False,
             "result": "DONE", "_echo_uuid": True, "_echo_session": True},
        ]])
        task, sent, directory = self.prepare_task()
        self.run_supervise(directory)
        self.join()
        receipt = self.settle(task, directory)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["native_outcome"], {
            "status": "completed", "error": None,
            "session_id": self._session(task)})
        self.assertNotIn("error", receipt)
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "succeeded")
        dep = self.app.store.get("dependency", sent["dependency_id"])
        self.assertEqual(dep["outcome"], "success")
        self.assertIn("DONE", dep.get("result_payload", {}).get("result_text", ""))

        # Raw succeeded can still be a business failure (existing rule).
        self.scenario([[
            {"type": "result", "subtype": "success", "is_error": False,
             "result": "DONE", "_echo_session": True},
        ]])
        task, sent, directory = self.prepare_task(required_outputs=["report.md"])
        self.run_supervise(directory)
        self.join()
        receipt = self.settle(task, directory)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["native_outcome"]["status"], "completed")
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "failed")
        self.assertEqual(settled["error"], "required_output_missing")
        self.assertEqual(settled["receipt"]["status"], "succeeded")
        dep = self.app.store.get("dependency", sent["dependency_id"])
        self.assertEqual(dep["outcome"], "failed")
        self.assertEqual(dep["result_payload"].get("missing_outputs"), ["report.md"])

        # Error then an unrecognized/missing-field result clears the failure and
        # falls back to the legacy process path.
        self.scenario([[
            {"type": "result", "is_error": True,
             "result": "EARLIER_FINAL", "_echo_session": True},
            {"type": "result", "_echo_session": True},
        ]])
        task, sent, directory = self.prepare_task()
        self.run_supervise(directory, idle=1.0)
        self.join()
        receipt = self.settle(task, directory)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertNotIn("native_outcome", receipt)
        self.assertNotIn("error", receipt)
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "succeeded")
        self.assertNotIn("native_outcome", settled["receipt"])
        self.assertEqual(self.app.store.get("dependency", sent["dependency_id"])["outcome"],
                         "success")

        # No result at all: the stub exits on its own and the legacy process
        # path still settles.
        self.scenario([], no_result=True)
        task, sent, directory = self.prepare_task()
        self.run_supervise(directory)
        self.join()
        receipt = self.settle(task, directory)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertNotIn("native_outcome", receipt)
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "succeeded")

    def test_native_outcome_preserves_process_and_cancel_priority(self):
        cases = [
            ("completed_nonzero_exit",
             {"type": "result", "subtype": "success", "is_error": False,
              "result": "DONE"}, 3, "completed"),
            ("failed_nonzero_exit",
             {"type": "result", "is_error": True, "result": "DONE"}, 7, "failed"),
        ]
        for name, item, code, native in cases:
            with self.subTest(name=name):
                self.scenario([[dict(item, _echo_session=True)]], exit_code=code)
                task, sent, directory = self.prepare_task()
                self.run_supervise(directory)
                self.join()
                receipt = runner.inspect(directory, include_tail=True)
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["exit_code"], code)
                self.assertEqual(receipt["native_outcome"]["status"], native)
                self.assertFalse(receipt["timed_out"])
                # The process fact decides; no native short code is asserted.
                self.assertNotIn("error", receipt)
                self.app.complete_task(task, receipt)
                self.assertEqual(self.app.store.get("task", task["id"])["status"], "failed")

        # Timeout outranks a native failure.
        marker = self.scenario([[
            {"type": "result", "is_error": True, "_echo_session": True},
        ]], sleep=30)
        task, sent, directory = self.prepare_task(timeout_s=2.0)
        self.run_supervise(directory)
        self.await_marker(marker, "frame-1")
        self.join()
        receipt = runner.inspect(directory, include_tail=True)
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["timed_out"])
        self.assertEqual(receipt["native_outcome"]["status"], "failed")
        self.assertNotIn("error", receipt)
        self.app.complete_task(task, receipt)
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "failed")

        # A real control-plane cancel is still cancelled, never native-failed.
        for item, native in (
                ({"type": "result", "subtype": "success", "is_error": False},
                 "completed"),
                ({"type": "result", "is_error": True}, "failed")):
            with self.subTest(cancel_native=native):
                marker = self.scenario([[dict(item, _echo_session=True)]], sleep=30)
                task, sent, directory = self.prepare_task()
                self.run_supervise(directory)
                self.await_marker(marker, "frame-1")
                (directory / "cancel").touch()
                self.join()
                receipt = runner.inspect(directory, include_tail=True)
                self.assertEqual(receipt["status"], "cancelled")
                self.assertFalse(receipt["timed_out"])
                self.assertEqual(receipt["native_outcome"]["status"], native)
                self.assertNotIn("error", receipt)
                self.app.complete_task(task, receipt)
                self.assertEqual(self.app.store.get("task", task["id"])["status"],
                                 "cancelled")

    def test_cleanup_failure_overrides_native_failure_with_unknown(self):
        cfg = dict(self.cfg)
        cfg["resources"] = [{
            "key": "release-fails",
            "acquire_argv": [sys.executable, "-c", "pass"],
            "release_argv": [sys.executable, "-c", "raise SystemExit(1)"],
        }]
        cfg["installations"] = [dict(self.cfg["installations"][0],
                                     resource_keys=["release-fails"])]
        self.app.store.close()
        self.app = Service(cfg, self.root / "data-unproven")
        self.app.store.put("probe", {
            "id": "codex-local", "available": True,
            "supervision": runner.PGROUP_COOPERATIVE})
        self.cfg = cfg
        self.scenario([[
            {"type": "result", "is_error": True, "result": "EARLIER_FINAL",
             "_echo_session": True},
        ]])
        task, sent, directory = self.prepare_task()
        self.run_supervise(directory)
        self.join()
        receipt = runner.inspect(directory, include_tail=True)
        self.assertEqual(receipt["status"], "unknown")
        self.assertFalse(receipt["process_tree_confirmed"])
        self.assertEqual(receipt["native_outcome"]["status"], "failed")
        self.assertEqual(receipt["native_outcome"]["error"], ERROR)
        with self.assertRaises(Fault):
            self.app.complete_task(task, receipt)
        self.assertNotIn(self.app.store.get("task", task["id"])["status"],
                         {"succeeded", "failed", "cancelled"})

    def test_native_error_does_not_copy_provider_or_prompt_text(self):
        long_text = SENTINEL + " " + ("X" * 5000)
        # An unknown future subtype with is_error=True still uses the fixed code.
        self.scenario([[
            {"type": "result", "is_error": True, "subtype": "error_future_kind",
             "error": {"message": long_text, "details": long_text},
             "errors": [{"message": long_text}],
             "result": SENTINEL + "-RESULT-TEXT",
             "_echo_session": True},
        ]])
        task, sent, directory = self.prepare_task(prompt=PROMPT_SENTINEL)
        self.run_supervise(directory)
        self.join()
        receipt = self.settle(task, directory)
        self.assertEqual(receipt["error"], ERROR)
        self.assertEqual(receipt["native_outcome"]["error"], ERROR)
        self.assertLessEqual(len(receipt["error"]), 64)
        self.assertLessEqual(len(receipt["native_outcome"]["error"]), 64)
        self.assertTrue(receipt["error"].isascii())
        self.assertNotIn(SENTINEL, json.dumps(receipt["native_outcome"]))
        self.assertNotIn(SENTINEL, receipt["error"])
        self.assertNotIn(PROMPT_SENTINEL, json.dumps(receipt["native_outcome"]))
        # The raw result text may stay as diagnostic evidence on the receipt...
        self.assertIn(SENTINEL, receipt["result_text"])
        # ...but it never reaches the settlement projection.
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "failed")
        self.assertNotIn(SENTINEL, json.dumps(settled["receipt"]["native_outcome"]))
        self.assertNotIn(SENTINEL, settled["receipt"]["error"])
        dep = self.app.store.get("dependency", sent["dependency_id"])
        self.assertNotIn(SENTINEL, json.dumps(dep.get("result_payload", {})))
        self.assertNotIn(SENTINEL, dep.get("summary", ""))
        self.assertNotIn(PROMPT_SENTINEL, json.dumps(dep.get("result_payload", {})))
        self.assertNotIn(PROMPT_SENTINEL, dep.get("summary", ""))
        messages = [m for m in self.app.store.find("message", work_id=task["work_id"])
                    if m.get("kind") == "execution_result"]
        self.assertTrue(messages)
        self.assertNotIn(SENTINEL, messages[-1].get("content", ""))


class ClaudeNativeOutcomeSettlementTests(_ServiceBase):
    STRONG = "windows-job-object"

    def _settled_task(self, *, required_outputs=None):
        sent = self.app.send("bot-main", {"text": "task", "allow_write": True},
                             "settle-" + uuid.uuid4().hex[:8])
        body = {"prompt": "task"}
        if required_outputs:
            body["required_outputs"] = required_outputs
        submitted = self.app.submit_task("bot-main", sent["work_id"], body)
        task = self.app.store.get("task", submitted["task_id"])
        task["attempt_uuid"] = "attempt-" + uuid.uuid4().hex
        self.app.store.put("task", task)
        return self.app.store.get("task", task["id"]), submitted

    def _receipt(self, task, **extra):
        receipt = {
            "execution_id": task["execution_id"],
            "spec_digest": task["spec_digest"],
            "attempt_uuid": task["attempt_uuid"],
            "status": "failed", "exit_code": 0,
            "error": ERROR,
            "native_outcome": {"status": "failed", "error": ERROR,
                               "session_id": SESSION},
            "process_tree_confirmed": True,
            "supervision": runner.PGROUP_COOPERATIVE,
            "termination_evidence": "pgroup-empty",
            "artifacts": [], "tail": "UNTRUSTED_DIAGNOSTIC",
        }
        receipt.update(extra)
        return receipt

    def test_native_failure_cannot_bypass_attempt_or_stop_proof(self):
        task, _ = self._settled_task()
        for name, extra in (
                ("execution_id", {"execution_id": "exec-" + "0" * 32}),
                ("spec_digest", {"spec_digest": "wrong"}),
                ("attempt_uuid", {"attempt_uuid": "a-different-attempt"}),
                ("no_cleanup_proof", {"process_tree_confirmed": False}),
                ("wrong_evidence", {"supervision": self.STRONG,
                                    "termination_evidence": "pgroup-empty"}),
                ("missing_evidence", {"termination_evidence": None})):
            with self.subTest(name=name):
                receipt = self._receipt(task)
                receipt.update(extra)
                if name == "missing_evidence":
                    receipt.pop("termination_evidence", None)
                with self.assertRaises(Fault):
                    self.app.complete_task(task, receipt)
                current = self.app.store.get("task", task["id"])
                self.assertNotIn(current["status"],
                                 {"succeeded", "failed", "cancelled"})

        if os.name != "posix":
            unproven = self._receipt(task, process_tree_confirmed=False)
            self.assertFalse(runner.receipt_proof_ok(
                unproven, task.get("supervision_floor")))
            return
        # A real failed execution whose cleanup is NOT proven stays unknown, and
        # the unknown receipt is refused by the existing settlement gate.
        cfg = dict(self.cfg)
        cfg["resources"] = [{
            "key": "release-fails",
            "acquire_argv": [sys.executable, "-c", "pass"],
            "release_argv": [sys.executable, "-c", "raise SystemExit(1)"],
        }]
        cfg["installations"] = [dict(self.cfg["installations"][0],
                                     resource_keys=["release-fails"])]
        self.app.store.close()
        self.app = Service(cfg, self.root / "data-unproven")
        self.app.store.put("probe", {
            "id": "codex-local", "available": True,
            "supervision": runner.PGROUP_COOPERATIVE})
        self.cfg = cfg
        self.scenario([[
            {"type": "result", "is_error": True, "_echo_session": True},
        ]])
        task, sent, directory = self.prepare_task()
        self.run_supervise(directory)
        self.join()
        receipt = runner.inspect(directory, include_tail=True)
        self.assertEqual(receipt["status"], "unknown")
        self.assertFalse(receipt["process_tree_confirmed"])
        self.assertEqual(receipt["native_outcome"]["status"], "failed")
        with self.assertRaises(Fault):
            self.app.complete_task(task, receipt)
        self.assertNotIn(self.app.store.get("task", task["id"])["status"],
                         {"succeeded", "failed", "cancelled"})

    def test_native_failure_cannot_fast_accept(self):
        task, _ = self._settled_task()
        task["access_mode"] = "verified_read"
        self.app.store.put("task", task)
        task = self.app.store.get("task", task["id"])
        self.assertFalse(task.get("outputs"))
        self.assertFalse(task.get("required_outputs"))
        gate = TaskResultGate(self.app, SemanticGate(self.app))
        succeeded = {
            "status": "succeeded", "process_tree_confirmed": True,
            "supervision": self.STRONG, "tail": "the answer is 42",
            "artifacts": [], "truncated": False,
        }
        self.assertTrue(gate.eligible(task, succeeded))
        failed = dict(succeeded, status="failed", error=ERROR,
                      native_outcome=self._receipt(task)["native_outcome"])
        self.assertFalse(gate.eligible(task, failed))

        calls = []
        gate.gate._call = lambda *a, **k: calls.append(1) or {
            "disposition": "ACCEPT", "confidence": 1.0}
        gate.mode = "enforce"
        self.assertIsNone(gate.decide(task, failed))
        self.assertEqual(calls, [])
        self.assertIsNone(self.app._fast_accept_decision(task, failed))

    def test_legacy_receipt_without_native_outcome_still_settles(self):
        task, sent = self._settled_task()
        succeeded = self._receipt(task, status="succeeded", exit_code=0,
                                  error=None, native_outcome=None,
                                  supervision=self.STRONG,
                                  termination_evidence=None)
        succeeded.pop("native_outcome")
        self.app.complete_task(task, succeeded)
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "succeeded")
        self.assertNotIn("native_outcome", settled["receipt"])
        self.assertEqual(self.app.store.get("dependency", sent["dependency_id"])["outcome"],
                         "success")

        task, sent = self._settled_task()
        failed = self._receipt(task, status="failed", exit_code=1,
                               error="executor_exit_1", native_outcome=None,
                               supervision=self.STRONG, termination_evidence=None)
        failed.pop("native_outcome")
        self.app.complete_task(task, failed)
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "failed")
        self.assertNotIn("native_outcome", settled["receipt"])
        dep = self.app.store.get("dependency", sent["dependency_id"])
        self.assertEqual(dep["outcome"], "failed")
        self.assertEqual(dep["summary"], "failed: executor_exit_1")


if __name__ == "__main__":
    unittest.main()
