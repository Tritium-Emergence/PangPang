"""Repair #78 P0-1: the Codex app-server native turn outcome.

A current turn that the server reports as failed/interrupted must not become a
successful receipt merely because the process exited 0. The receipt gains an
optional `native_outcome` (identity + terminal status + one fixed short code);
the real `exit_code` is preserved. Higher-priority facts (control-plane cancel,
timeout, non-zero exit, unconfirmed cleanup) keep their existing precedence.

The fold tests drive `apply_codex_event` directly. The supervise tests run a
local app-server stub through the real runner and the existing Service
settlement path. No real Codex, Claude, Jev, or external service is used.
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


THREAD = "thr_from_server"
SENTINEL = "SENTINEL_PROVIDER_TEXT_DO_NOT_LEAK"
PROMPT_SENTINEL = "SENTINEL_PROMPT_DO_NOT_LEAK"


def _state():
    state = {"result_text": None, "confirmed": set()}
    runner.apply_codex_event(state, {
        "id": 1, "result": {"thread": {"id": THREAD, "forkedFromId": None}}})
    return state


def _start(state, turn_id, *, outer=None, inner=None):
    turn = {"id": turn_id, "status": "inProgress"}
    if inner is not None:
        turn["threadId"] = inner
    params = {"turn": turn}
    if outer is not None:
        params["threadId"] = outer
    runner.apply_codex_event(state, {"method": "turn/started", "params": params})


def _complete(state, turn_id, status, *, outer=None, inner=None):
    turn = {"id": turn_id, "status": status}
    if inner is not None:
        turn["threadId"] = inner
    params = {"turn": turn}
    if outer is not None:
        params["threadId"] = outer
    runner.apply_codex_event(state, {"method": "turn/completed", "params": params})


class NativeOutcomeFoldTests(unittest.TestCase):
    def test_terminal_outcome_requires_current_identity(self):
        expected = {
            "completed": None,
            "failed": "codex_turn_failed",
            "interrupted": "codex_turn_interrupted",
        }
        for status, error in expected.items():
            with self.subTest(status=status):
                state = _state()
                _start(state, "turn_1")
                _complete(state, "turn_1", status)
                self.assertEqual(state["native_outcome"], {
                    "thread_id": THREAD, "turn_id": "turn_1",
                    "status": status, "error": error,
                })
                self.assertEqual(state["result_seq"], 1)
                self.assertFalse(state["turn_active"])

        # The existing protocol shape omits threadId: it still matches the bound
        # thread, but only the current turn.
        state = _state()
        _start(state, "turn_1", outer=THREAD, inner=THREAD)
        _complete(state, "turn_1", "failed", outer=THREAD, inner=THREAD)
        self.assertEqual(state["native_outcome"]["status"], "failed")

        # Any provided owner that conflicts or is malformed invalidates the event.
        for outer, inner in (("thr_other", None), (None, "thr_other"),
                             (THREAD, "thr_other"), ("", None), (123, None)):
            with self.subTest(outer=outer, inner=inner):
                state = _state()
                _start(state, "turn_1")
                _complete(state, "turn_1", "failed", outer=outer, inner=inner)
                self.assertIsNone(state.get("native_outcome"))
                self.assertEqual(state.get("result_seq", 0), 0)
                self.assertTrue(state["turn_active"])

        # A completion cannot adopt an identity: missing, foreign, or no current
        # turn means no fact and no idle-close advance.
        for turn_id, status in (("", "failed"), (None, "failed"),
                                ("turn_other", "failed")):
            with self.subTest(turn_id=turn_id):
                state = _state()
                _start(state, "turn_1")
                runner.apply_codex_event(state, {"method": "turn/completed", "params": {
                    "turn": {"id": turn_id, "status": status}}})
                self.assertIsNone(state.get("native_outcome"))
                self.assertEqual(state.get("result_seq", 0), 0)
                self.assertTrue(state["turn_active"])

        fresh = _state()
        _complete(fresh, "turn_1", "failed")
        self.assertIsNone(fresh.get("native_outcome"))
        self.assertEqual(fresh.get("result_seq", 0), 0)

    def test_explicit_null_owner_is_malformed_not_absent(self):
        # SPEC line 43: an omitted threadId is compatible; a supplied null is
        # malformed and must leave the accepted turn state untouched.
        malformed_starts = {
            "started_outer_null": {
                "method": "turn/started",
                "params": {"threadId": None,
                           "turn": {"id": "turn_bad", "status": "inProgress"}}},
            "started_inner_null": {
                "method": "turn/started",
                "params": {"turn": {"id": "turn_bad", "threadId": None,
                                    "status": "inProgress"}}},
            "response_inner_null": {
                "id": 9001,
                "result": {"turn": {"id": "turn_bad", "threadId": None,
                                    "status": "inProgress"}}},
        }
        for name, event in malformed_starts.items():
            with self.subTest(name=name):
                state = _state()
                _start(state, "turn_A")
                _complete(state, "turn_A", "failed")
                outcome = dict(state["native_outcome"])
                # The malformed start neither adopts turn_bad nor clears the
                # recorded failure or advances the completion counter.
                runner.apply_codex_event(state, event)
                self.assertEqual(state["turn_id"], "turn_A")
                self.assertEqual(state["native_outcome"], outcome)
                self.assertEqual(state["result_seq"], 1)
                self.assertFalse(state["turn_active"])
                self.assertNotIn("turn_bad", state.get("turn_seen", set()))
                # The unfamiliar completion is ignored, so the failure stands.
                runner.apply_codex_event(state, {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn_bad", "status": "completed"}}})
                self.assertEqual(state["turn_id"], "turn_A")
                self.assertEqual(state["native_outcome"], outcome)
                self.assertEqual(state["result_seq"], 1)

        # On a fresh state a supplied-null owner cannot adopt an identity.
        for name, event in malformed_starts.items():
            with self.subTest(name=name, fresh=True):
                state = _state()
                runner.apply_codex_event(state, event)
                self.assertIsNone(state.get("turn_id"))
                self.assertIsNone(state.get("native_outcome"))
                self.assertEqual(state.get("result_seq", 0), 0)
                self.assertFalse(state.get("turn_active", False))

        # A completion with a supplied-null owner is equally inert.
        for name, params in (
                ("completed_outer_null", {
                    "threadId": None,
                    "turn": {"id": "turn_A", "status": "failed"}}),
                ("completed_inner_null", {
                    "turn": {"id": "turn_A", "threadId": None,
                             "status": "failed"}})):
            with self.subTest(name=name):
                state = _state()
                _start(state, "turn_A")
                runner.apply_codex_event(state, {
                    "method": "turn/completed", "params": params})
                self.assertIsNone(state.get("native_outcome"))
                self.assertEqual(state.get("result_seq", 0), 0)
                self.assertTrue(state["turn_active"])

        # Omitting the key remains compatible: the start response and events
        # without any threadId still belong to the bound thread.
        state = _state()
        runner.apply_codex_event(state, {
            "id": 11, "result": {"turn": {"id": "turn_1", "status": "inProgress"}}})
        self.assertEqual(state["turn_id"], "turn_1")
        _complete(state, "turn_1", "failed")
        self.assertEqual(state["native_outcome"]["status"], "failed")

    def test_duplicate_completion_advances_once_and_failure_wins(self):
        state = _state()
        _start(state, "turn_1")
        _complete(state, "turn_1", "completed")
        _complete(state, "turn_1", "completed")
        self.assertEqual(state["result_seq"], 1)
        self.assertEqual(state["native_outcome"]["status"], "completed")

        # A duplicate start (response and event) does not reopen the turn or
        # clear the recorded outcome.
        _start(state, "turn_1")
        runner.apply_codex_event(state, {
            "id": 7, "result": {"turn": {"id": "turn_1", "status": "inProgress"}}})
        self.assertFalse(state["turn_active"])
        self.assertEqual(state["native_outcome"]["status"], "completed")
        self.assertEqual(state["result_seq"], 1)

        # Conflicting terminal notifications merge conservatively.
        for first, second, final in (
                ("completed", "failed", "failed"),
                ("failed", "completed", "failed"),
                ("completed", "interrupted", "interrupted"),
                ("interrupted", "completed", "interrupted"),
                ("interrupted", "failed", "failed"),
                ("failed", "interrupted", "failed")):
            with self.subTest(first=first, second=second):
                state = _state()
                _start(state, "turn_1")
                _complete(state, "turn_1", first)
                _complete(state, "turn_1", second)
                self.assertEqual(state["native_outcome"]["status"], final)
                self.assertEqual(state["result_seq"], 1)
                self.assertEqual(state["native_outcome"]["error"],
                                 "codex_turn_failed" if final == "failed"
                                 else "codex_turn_interrupted")

    def test_stale_and_foreign_events_preserve_current_failure(self):
        state = _state()
        _start(state, "turn_A")
        _complete(state, "turn_A", "completed")
        _start(state, "turn_B")
        self.assertIsNone(state.get("native_outcome")) # new legal turn clears A
        # A different turn is not adopted while the current one is active.
        _start(state, "turn_D", outer=THREAD)
        self.assertEqual(state["turn_id"], "turn_B")
        self.assertTrue(state["turn_active"])
        _complete(state, "turn_B", "failed")

        # Late completion of the old turn, an unknown turn, a different outer
        # thread, and a foreign inner thread change nothing.
        _complete(state, "turn_A", "completed")
        _complete(state, "turn_C", "completed")
        _complete(state, "turn_B", "failed", outer="thr_other")
        _complete(state, "turn_B", "failed", inner="thr_other")
        self.assertEqual(state["native_outcome"]["turn_id"], "turn_B")
        self.assertEqual(state["native_outcome"]["status"], "failed")
        self.assertEqual(state["result_seq"], 2)

        # A late start for the old, already-seen turn is rejected. A different
        # new turn is also rejected while the current turn is active.
        _start(state, "turn_A")
        self.assertEqual(state["turn_id"], "turn_B")
        self.assertEqual(state["native_outcome"]["status"], "failed")
        runner.apply_codex_event(state, {
            "id": 9, "result": {"turn": {"id": "turn_A", "status": "inProgress"}}})
        self.assertEqual(state["turn_id"], "turn_B")
        self.assertFalse(state["turn_active"])

        # A different thread is never adopted once one is bound.
        runner.apply_codex_event(state, {"method": "thread/started", "params": {
            "thread": {"id": "thr_other", "forkedFromId": None}}})
        self.assertEqual(state["thread_id"], THREAD)

    def test_new_turn_clears_old_failure_without_stale_poisoning(self):
        for failed in ("failed", "interrupted"):
            with self.subTest(failed=failed):
                state = _state()
                state["result_text"] = "OLD_FINAL"
                _start(state, "turn_A")
                _complete(state, "turn_A", failed)
                self.assertEqual(state["native_outcome"]["status"], failed)

                _start(state, "turn_B")
                self.assertIsNone(state.get("native_outcome"))
                self.assertTrue(state["turn_active"])
                self.assertEqual(state["turn_id"], "turn_B")
                self.assertEqual(state["result_seq"], 1) # history is not reset
                self.assertEqual(state["result_text"], "OLD_FINAL")

                _complete(state, "turn_B", "completed")
                self.assertEqual(state["native_outcome"]["status"], "completed")
                self.assertIsNone(state["native_outcome"]["error"])
                self.assertEqual(state["result_seq"], 2)

                # The late A failure cannot poison B.
                _complete(state, "turn_A", failed)
                self.assertEqual(state["native_outcome"]["turn_id"], "turn_B")
                self.assertEqual(state["native_outcome"]["status"], "completed")
                self.assertEqual(state["result_seq"], 2)

    def test_missing_malformed_unknown_status_is_not_native_success(self):
        for status in (None, 1, "", "weird", [], {"status": "failed"}):
            with self.subTest(status=status):
                state = _state()
                _start(state, "turn_1")
                turn = {"id": "turn_1"}
                if status is not None:
                    turn["status"] = status
                runner.apply_codex_event(state, {
                    "method": "turn/completed", "params": {"turn": turn}})
                # Not a native success: no outcome is invented.
                self.assertIsNone(state.get("native_outcome"))
                # The idle-close counter still advances exactly once.
                self.assertEqual(state["result_seq"], 1)
                self.assertFalse(state["turn_active"])
                runner.apply_codex_event(state, {
                    "method": "turn/completed", "params": {"turn": turn}})
                self.assertEqual(state["result_seq"], 1)
                self.assertIsNone(state.get("native_outcome"))

        # An unknown-status duplicate does not clear a known failure.
        state = _state()
        _start(state, "turn_1")
        _complete(state, "turn_1", "failed")
        runner.apply_codex_event(state, {"method": "turn/completed", "params": {
            "turn": {"id": "turn_1"}}})
        self.assertEqual(state["native_outcome"]["status"], "failed")
        self.assertEqual(state["result_seq"], 1)


CODEX_SCENARIO_STUB = r"""
import json, os, pathlib, sys, time
base = pathlib.Path(__file__).resolve().parent
scenario = json.loads((base / "scenario.json").read_text(encoding="utf-8"))
thread_id = scenario.get("thread_id", "thr_from_server")
turns = scenario.get("turns") or []
marker = scenario.get("marker", "stage")
seen = []
turn_index = 0

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def mark(name, data=""):
    (base / (marker + "-" + name)).write_text(data, encoding="utf-8")

def echo(client_id, content):
    emit({"method": "item/completed", "params": {"item": {
        "type": "userMessage", "id": "um-%d" % len(seen),
        "clientId": client_id, "content": content}}})

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
        emit({"method": "thread/started", "params": {
            "thread": {"id": thread_id, "forkedFromId": None}}})
    elif method == "turn/start" and "id" in msg:
        turn_index += 1
        conf = turns[turn_index - 1] if turn_index - 1 < len(turns) else {}
        tid = conf.get("id", "turn_%d" % turn_index)
        response_turn = {"id": tid, "status": "inProgress"}
        if "response_inner_thread" in conf:
            response_turn["threadId"] = conf["response_inner_thread"]
        emit({"id": msg["id"], "result": {"turn": response_turn}})
        started = {"id": tid, "status": "inProgress"}
        if "inner_thread" in conf:
            started["threadId"] = conf["inner_thread"]
        params = {"turn": started}
        params["threadId"] = conf.get("outer_thread", thread_id)
        emit({"method": "turn/started", "params": params})
        p = msg.get("params") or {}
        echo(p.get("clientUserMessageId"), p.get("input"))
        for text in conf.get("finals") or []:
            emit({"method": "item/completed", "params": {"item": {
                "type": "agentMessage", "id": "am-%d" % len(seen),
                "text": text, "phase": "final_answer"}}})
        if conf.get("write_artifact"):
            out = os.environ.get("XPP_OUTPUT_DIR")
            if out:
                pathlib.Path(out, conf["write_artifact"]).write_text("artifact", encoding="utf-8")
        if conf.get("pre_completion_delay"):
            time.sleep(float(conf["pre_completion_delay"]))
        if conf.get("completion", True):
            terminal = {"id": tid}
            if not conf.get("omit_status"):
                terminal["status"] = conf.get("status", "completed")
            if conf.get("error") is not None:
                terminal["error"] = conf["error"]
            cparams = {"turn": terminal}
            if conf.get("outer_thread_completed") is not None:
                cparams["threadId"] = conf["outer_thread_completed"]
            for index in range(int(conf.get("duplicates", 1))):
                if index and conf.get("duplicate_status"):
                    terminal["status"] = conf["duplicate_status"]
                emit({"method": "turn/completed", "params": cparams})
        for raw in conf.get("after_events") or []:
            emit(raw)
        mark("turn-%d" % turn_index, json.dumps({"turn": tid}))
        if conf.get("sleep") is not None:
            time.sleep(float(conf["sleep"]))
        if conf.get("exit") is not None:
            sys.exit(int(conf["exit"]))
    elif method == "turn/steer" and "id" in msg:
        p = msg.get("params") or {}
        emit({"id": msg["id"], "result": {"turnId": p.get("expectedTurnId")}})
        echo(p.get("clientUserMessageId"), p.get("input"))
        emit({"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "id": "am-steer",
            "text": "steer commentary", "phase": "commentary"}}})
    elif method == "turn/interrupt" and "id" in msg:
        emit({"id": msg["id"], "result": {}})
mark("stdin-eof", json.dumps(seen))
"""


@unittest.skipUnless(os.name == "posix", "process-group supervise is POSIX")
class _SuperviseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.stub = self.root / "stub_codex.py"
        self.stub.write_text(CODEX_SCENARIO_STUB, encoding="utf-8")
        cfg = config(self.workspace)
        cfg["installations"][0]["argv"] = [
            sys.executable, str(self.stub), "exec",
            "--sandbox", "workspace-write", "-"]
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

    def scenario(self, turns, *, marker=None):
        marker = marker or ("stage-" + uuid.uuid4().hex[:8])
        (self.root / "scenario.json").write_text(json.dumps({
            "thread_id": THREAD, "turns": turns, "marker": marker,
        }), encoding="utf-8")
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


class NativeOutcomeSuperviseTests(_SuperviseBase):
    def _assert_failed_receipt(self, receipt, error):
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["error"], error)
        self.assertEqual(receipt["native_outcome"]["thread_id"], THREAD)
        self.assertEqual(receipt["native_outcome"]["error"], error)
        self.assertFalse(receipt["timed_out"])
        self.assertTrue(receipt["process_tree_confirmed"])

    def _assert_failed_settlement(self, task, sent, receipt, old_text):
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "failed")
        self.assertEqual(settled["receipt"]["status"], "failed")
        self.assertEqual(settled["receipt"]["exit_code"], 0)
        self.assertEqual(settled["receipt"]["native_outcome"]["status"], "failed")
        dep = self.app.store.get("dependency", sent["dependency_id"])
        self.assertEqual(dep["outcome"], "failed")
        self.assertTrue(dep["summary"].startswith("failed"))
        self.assertIn("codex_turn_failed", dep["summary"])
        payload = dep.get("result_payload", {})
        self.assertEqual(payload.get("status"), "failed")
        self.assertEqual(payload.get("error"), "codex_turn_failed")
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
        # (a) The same turn writes a final answer and then reports failed.
        marker = self.scenario([{
            "finals": ["EARLIER_FINAL"], "status": "failed",
            "write_artifact": "report.md"}])
        task, sent, directory = self.prepare_task()
        self.run_supervise(directory)
        self.join()
        receipt = self.settle(task, directory)
        self._assert_failed_receipt(receipt, "codex_turn_failed")
        self.assertEqual(receipt["native_outcome"]["turn_id"], "turn_1")
        self.assertEqual(receipt["result_text"], "EARLIER_FINAL")
        self._assert_failed_settlement(task, sent, receipt, "EARLIER_FINAL")
        # Re-submitting the same receipt is inert: no second settle, no extra
        # message or artifact.
        messages = len(self.app.store.find("message", work_id=task["work_id"]))
        artifacts = len(self.app.store.find("artifact"))
        stored = self.app.store.get("task", task["id"])
        self.app.complete_task(stored, receipt)
        self.assertEqual(messages, len(self.app.store.find("message", work_id=task["work_id"])))
        self.assertEqual(artifacts, len(self.app.store.find("artifact")))
        self.assertEqual(
            self.app.store.get("dependency", sent["dependency_id"])["outcome"], "failed")

        # (b) A succeeds and B (started by an explicit inject) fails: A's old
        # final must not travel to the dependency or the execution_result.
        marker = self.scenario([
            # Hold A open briefly so the session observes turn_active and can
            # take the next mailbox row after it completes.
            {"finals": ["A_FINAL"], "status": "completed",
             "pre_completion_delay": 0.4},
            {"finals": ["B_FINAL"], "status": "failed",
             "write_artifact": "b-report.md"},
        ])
        task, sent, directory = self.prepare_task()
        self.run_supervise(directory, idle=2.0)
        self.await_marker(marker, "turn-1")
        time.sleep(0.2) # let the reader fold A before the session looks again
        runner.append_inject(directory, str(uuid.uuid4()), "second turn")
        self.join()
        receipt = self.settle(task, directory)
        self._assert_failed_receipt(receipt, "codex_turn_failed")
        self.assertEqual(receipt["native_outcome"]["turn_id"], "turn_2")
        self._assert_failed_settlement(task, sent, receipt, "A_FINAL")
        payload = self.app.store.get("dependency", sent["dependency_id"])["result_payload"]
        self.assertNotIn("B_FINAL", json.dumps(payload))
        starts = [m for m in self.stdin_messages(marker) if m.get("method") == "turn/start"]
        self.assertEqual(len(starts), 2)
        self.assertFalse(any(m.get("method") == "turn/interrupt"
                             for m in self.stdin_messages(marker)))

    def test_malformed_null_owner_start_cannot_erase_failed_outcome(self):
        # A valid turn fails; a start for `turn_bad` that supplies a null owner
        # (outer, inner, or in the turn response) must not adopt the identity or
        # clear the failure. Its completion must stay unfamiliar, so the run
        # settles failed and the old final never reaches the dependency.
        cases = {
            "started_outer_null": {
                "method": "turn/started",
                "params": {"threadId": None,
                           "turn": {"id": "turn_bad", "status": "inProgress"}}},
            "started_inner_null": {
                "method": "turn/started",
                "params": {"turn": {"id": "turn_bad", "threadId": None,
                                    "status": "inProgress"}}},
            "response_inner_null": {
                "id": 9001,
                "result": {"turn": {"id": "turn_bad", "threadId": None,
                                    "status": "inProgress"}}},
        }
        for name, event in cases.items():
            with self.subTest(name=name):
                marker = self.scenario([{
                    "finals": ["EARLIER_FINAL"], "status": "failed",
                    "write_artifact": "report.md",
                    "after_events": [event, {
                        "method": "turn/completed",
                        "params": {"turn": {"id": "turn_bad",
                                            "status": "completed"}}}]}])
                task, sent, directory = self.prepare_task()
                self.run_supervise(directory)
                self.join()
                receipt = self.settle(task, directory)
                self._assert_failed_receipt(receipt, "codex_turn_failed")
                self.assertEqual(receipt["native_outcome"]["turn_id"], "turn_1")
                self.assertEqual(receipt["native_outcome"]["status"], "failed")
                self.assertEqual(receipt["result_text"], "EARLIER_FINAL")
                self._assert_failed_settlement(task, sent, receipt, "EARLIER_FINAL")
                # The malformed start did not trigger a new outgoing turn.
                starts = [m for m in self.stdin_messages(marker)
                          if m.get("method") == "turn/start"]
                self.assertEqual(len(starts), 1)

    def test_interrupted_exit_zero_is_failed_without_cancel(self):
        marker = self.scenario([{"status": "interrupted"}])
        task, sent, directory = self.prepare_task()
        self.run_supervise(directory)
        self.join()
        receipt = self.settle(task, directory)
        self._assert_failed_receipt(receipt, "codex_turn_interrupted")
        self.assertEqual(receipt["native_outcome"]["status"], "interrupted")
        self.assertNotEqual(receipt["status"], "cancelled")
        self.assertFalse(receipt["timed_out"])
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "failed")
        dep = self.app.store.get("dependency", sent["dependency_id"])
        self.assertEqual(dep["outcome"], "failed")
        self.assertIn("codex_turn_interrupted", dep["summary"])
        self.assertNotIn("result_text", dep.get("result_payload", {}))
        # The failure itself starts nothing: one thread, one turn, no interrupt.
        messages = self.stdin_messages(marker)
        self.assertEqual(sum(m.get("method") == "thread/start" for m in messages), 1)
        self.assertEqual(sum(m.get("method") == "turn/start" for m in messages), 1)
        self.assertFalse(any(m.get("method") == "turn/interrupt" for m in messages))

    def test_completed_exit_zero_preserves_output_adjudication(self):
        # Positive control: completed + exit 0 is still a successful execution.
        self.scenario([{"finals": ["DONE"], "status": "completed"}])
        task, sent, directory = self.prepare_task()
        self.run_supervise(directory)
        self.join()
        receipt = self.settle(task, directory)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["native_outcome"]["status"], "completed")
        self.assertIsNone(receipt["native_outcome"]["error"])
        self.assertNotIn("error", receipt)
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "succeeded")
        dep = self.app.store.get("dependency", sent["dependency_id"])
        self.assertEqual(dep["outcome"], "success")
        self.assertIn("DONE", dep.get("result_payload", {}).get("result_text", ""))

        # raw succeeded can still be a business failure (existing rule).
        self.scenario([{"finals": ["DONE"], "status": "completed"}])
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

        # Unknown/missing native status keeps the legacy process path: success
        # on exit 0, and no invented native_outcome.
        self.scenario([{"omit_status": True}])
        task, sent, directory = self.prepare_task()
        self.run_supervise(directory)
        self.join()
        receipt = self.settle(task, directory)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertNotIn("native_outcome", receipt)
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "succeeded")
        self.assertNotIn("native_outcome", settled["receipt"])

    def test_native_outcome_preserves_process_and_cancel_priority(self):
        cases = [
            ("completed_nonzero_exit", {"status": "completed", "exit": 3},
             "failed", "completed", False),
            ("failed_nonzero_exit", {"status": "failed", "exit": 7},
             "failed", "failed", False),
        ]
        for name, turn, status, native, timed_out in cases:
            with self.subTest(name=name):
                self.scenario([turn])
                task, sent, directory = self.prepare_task()
                self.run_supervise(directory)
                self.join()
                receipt = runner.inspect(directory, include_tail=True)
                self.assertEqual(receipt["status"], status)
                self.assertNotEqual(receipt["exit_code"], 0)
                self.assertEqual(receipt["native_outcome"]["status"], native)
                self.assertFalse(receipt["timed_out"])
                # The process fact decides; no native short code is asserted.
                self.assertNotIn("error", receipt)
                self.app.complete_task(task, receipt)
                self.assertEqual(self.app.store.get("task", task["id"])["status"], "failed")

        # Timeout outranks a native terminal status.
        marker = self.scenario([{"status": "interrupted", "sleep": 30}])
        task, sent, directory = self.prepare_task(timeout_s=2.0)
        self.run_supervise(directory)
        self.await_marker(marker, "turn-1")
        self.join()
        receipt = runner.inspect(directory, include_tail=True)
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["timed_out"])
        self.assertEqual(receipt["native_outcome"]["status"], "interrupted")
        self.assertNotIn("error", receipt)
        self.app.complete_task(task, receipt)
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "failed")

        # A real control-plane cancel is still cancelled, never native-failed.
        for native in ("completed", "failed"):
            with self.subTest(cancel_native=native):
                marker = self.scenario([{"status": native, "sleep": 30}])
                task, sent, directory = self.prepare_task()
                self.run_supervise(directory)
                self.await_marker(marker, "turn-1")
                (directory / "cancel").touch()
                self.join()
                receipt = runner.inspect(directory, include_tail=True)
                self.assertEqual(receipt["status"], "cancelled")
                self.assertFalse(receipt["timed_out"])
                self.assertEqual(receipt["native_outcome"]["status"], native)
                self.assertNotIn("error", receipt)
                self.app.complete_task(task, receipt)
                self.assertEqual(self.app.store.get("task", task["id"])["status"], "cancelled")
                self.assertFalse(self.app.store.get("task", task["id"])["receipt"].get("timed_out"))

    def test_native_error_is_bounded_and_does_not_copy_provider_text(self):
        long_error = {"message": SENTINEL + " " + ("X" * 5000),
                      "additionalDetails": SENTINEL + "-details"}
        self.scenario([{"status": "failed", "error": long_error}])
        task, sent, directory = self.prepare_task(prompt=PROMPT_SENTINEL)
        self.run_supervise(directory)
        self.join()
        receipt = self.settle(task, directory)
        self.assertEqual(receipt["error"], "codex_turn_failed")
        self.assertEqual(receipt["native_outcome"]["error"], "codex_turn_failed")
        self.assertLessEqual(len(receipt["error"]), 64)
        self.assertLessEqual(len(receipt["native_outcome"]["error"]), 64)
        self.assertNotIn(SENTINEL, json.dumps(receipt["native_outcome"]))
        self.assertNotIn(SENTINEL, receipt["error"])
        self.assertNotIn(PROMPT_SENTINEL, json.dumps(receipt["native_outcome"]))
        settled = self.app.store.get("task", task["id"])
        self.assertEqual(settled["status"], "failed")
        self.assertNotIn(SENTINEL, json.dumps(settled["receipt"]["native_outcome"]))
        self.assertNotIn(SENTINEL, settled["receipt"]["error"])
        dep = self.app.store.get("dependency", sent["dependency_id"])
        self.assertNotIn(SENTINEL, json.dumps(dep.get("result_payload", {})))
        self.assertNotIn(SENTINEL, dep.get("summary", ""))
        self.assertNotIn(PROMPT_SENTINEL, json.dumps(dep.get("result_payload", {})))
        self.assertNotIn(PROMPT_SENTINEL, dep.get("summary", ""))


class NativeOutcomeSettlementTests(_SuperviseBase):
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
            "error": "codex_turn_failed",
            "native_outcome": {"thread_id": THREAD, "turn_id": "turn_1",
                               "status": "failed", "error": "codex_turn_failed"},
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

        # A failed execution whose cleanup is NOT proven stays unknown, and the
        # unknown receipt is refused by the existing settlement gate.
        if os.name != "posix":
            unproven = self._receipt(task, process_tree_confirmed=False)
            self.assertFalse(runner.receipt_proof_ok(
                unproven, task.get("supervision_floor")))
            return
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
        marker = self.scenario([{"status": "failed"}])
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
        failed = dict(succeeded, status="failed", error="codex_turn_failed",
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
