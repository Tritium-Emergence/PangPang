""": a withdrawn required-output contract must not stay pending.

(a) fails on: cancel_dependency withdraws a running task that declared
required_outputs=["a.md"]; the cancelled receipt then arrives on the live
settle path and is booked like a failure. A later task succeeds with a
different file, but pending_required_outputs stays ["a.md"] and the Work
ends blocked (required_output_missing), so no delivery is sent.

(b): a task that FAILS its required_outputs=["a.md"]
contract, followed by a success that does not declare a.md, still blocks.
"""
import tempfile
import unittest
from pathlib import Path

from test_bot_runtime import config
from pangpang.service import Service


class CancelledContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")
        self.app.probe("codex-local")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _receipt(self, task, status, results, *, exit_code=0):
        receipt = {
            "execution_id": task["execution_id"],
            "spec_digest": task["spec_digest"],
            "status": status,
            "process_tree_confirmed": True,
            "supervision": "windows-job-object",
            "exit_code": exit_code,
            "artifacts": [],
            "required_output_results": results,
            "tail": "done",
        }
        if task.get("attempt_uuid"):
            receipt["attempt_uuid"] = task["attempt_uuid"]
        return receipt

    def _finish_queued(self, work_id, content):
        turn = next(iter(self.app._all("turn", work_id=work_id, status="queued")), None)
        self.assertIsNotNone(turn, "expected a queued continuation turn")
        for message in self.app._work_inbox(work_id):
            message["consumed_by_turn_id"] = turn["id"]
            self.app.store.put("message", message)
        self.app.finish_turn(turn, content)

    def test_withdrawn_required_output_does_not_block_later_delivery(self):
        # A running contract is withdrawn, then its cancelled receipt arrives.
        # The replacement task produces a different file. The withdrawn path
        # must not remain pending, and the delivery reply must deliver.
        sent = self.app.send("bot-main", {"text": "做一份计划", "allow_write": True}, "corr")
        work_id = sent["work_id"]
        first = self.app.submit_task("bot-main", work_id, {
            "prompt": "写行动计划", "required_outputs": ["a.md"]})
        task = self.app.store.get("task", first["task_id"])
        task["status"] = "running"
        self.app.store.put("task", task)
        turn = self.app.store.get("turn", sent["turn_id"])
        turn["status"] = "running"
        self.app.store.put("turn", turn)

        withdrawn = self.app.cancel_dependency("bot-main", turn["id"], first["dependency_id"])
        self.assertEqual(withdrawn["status"], "cancelled")
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "cancel_requested")

        second = self.app.submit_task("bot-main", work_id, {
            "prompt": "改做风险清单", "required_outputs": ["b.md"]})
        cancelled = self.app.store.get("task", task["id"])
        self.app.complete_task(cancelled, self._receipt(
            cancelled, "cancelled",
            [{"output": "a.md", "present": False, "detail": "missing_or_empty"}],
            exit_code=-15))
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "cancelled")

        # The correction turn ends while the replacement is still running.
        # Delivery is the later reply, after that task succeeds.
        self.app.finish_turn(turn, "已撤回，按新要求重做。")
        self.assertEqual(self.app.store.get("work", work_id)["status"], "waiting")

        task2 = self.app.store.get("task", second["task_id"])
        self.app.complete_task(task2, self._receipt(
            task2, "succeeded", [{"output": "b.md", "present": True}]))
        self.assertEqual(self.app.store.get("task", task2["id"])["status"], "succeeded")

        self._finish_queued(work_id, "风险清单已交付。")
        work = self.app.store.get("work", work_id)
        self.assertEqual(
            (work.get("pending_required_outputs") or [], work["status"], work.get("error")),
            ([], "delivered", None))
        notes = [n for n in self.app.store.find("notification", bot_id="bot-main")
                 if n.get("work_id") == work_id and str(n.get("title", "")).startswith("已回复")]
        self.assertEqual(len(notes), 1)

    def test_failed_required_output_still_blocks_when_later_success_omits_it(self):
        # Fail-closed: a real failure of a.md is not cleared by a later success
        # that does not declare a.md in required_outputs.
        sent = self.app.send("bot-main", {"text": "做一份计划", "allow_write": True}, "fail")
        work_id = sent["work_id"]
        first = self.app.submit_task("bot-main", work_id, {
            "prompt": "写行动计划", "required_outputs": ["a.md"]})
        task = self.app.store.get("task", first["task_id"])
        self.app.complete_task(task, self._receipt(
            task, "succeeded",
            [{"output": "a.md", "present": False, "detail": "missing_or_empty"}]))
        failed = self.app.store.get("task", task["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "required_output_missing")

        second = self.app.submit_task("bot-main", work_id, {
            "prompt": "另写一份", "required_outputs": ["b.md"]})
        task2 = self.app.store.get("task", second["task_id"])
        self.app.complete_task(task2, self._receipt(
            task2, "succeeded", [{"output": "b.md", "present": True}]))
        self.assertEqual(self.app.store.get("task", task2["id"])["status"], "succeeded")
        self.assertNotIn("a.md", task2.get("required_outputs") or [])

        for _ in range(4):
            turn = next(iter(self.app._all("turn", work_id=work_id, status="queued")), None)
            if turn is None:
                break
            for message in self.app._work_inbox(work_id):
                message["consumed_by_turn_id"] = turn["id"]
                self.app.store.put("message", message)
            self.app.finish_turn(turn, "另一份文件写好了。")
        work = self.app.store.get("work", work_id)
        self.assertEqual(work.get("pending_required_outputs"), ["a.md"])
        self.assertEqual(work["status"], "blocked")
        self.assertEqual(work["error"], "required_output_missing")
        self.assertNotEqual(work["status"], "delivered")
