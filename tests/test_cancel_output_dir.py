""" tier-3: a cancelled running task names the directory the runner writes.

(a) fails on: cancel_dependency of a running task returns no output_dir.
(b) a pending task that never started returns no output_dir.
(c) a child_work cancel stays the same result.
(d) a tier-3 running-task correction still cancels first, then submits after the return and names output_dir.
"""
import tempfile
import unittest
from pathlib import Path

from test_bot_runtime import config
from pangpang.context import projection
from pangpang.runner import checked
from pangpang.service import Service


class CancelOutputDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")
        self.app.probe("codex-local")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _task(self, key, prompt="写一份草稿"):
        sent = self.app.send("bot-main", {"text": "执行", "allow_write": True}, key)
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": prompt})
        task = self.app.store.get("task", submitted["task_id"])
        dep = self.app.store.get("dependency", task["dependency_id"])
        turn = self.app.store.get("turn", sent["turn_id"])
        turn["status"] = "running"
        self.app.store.put("turn", turn)
        return sent, task, dep, turn

    def _runner_output_dir(self, task):
        """The candidate supervise() mkdirs: checked(cwd)/.xpp-out/<execution_id>."""
        roots = self.app.execution.hosts[task["host_id"]]["allowed_roots"]
        cwd = checked(task["cwd"], roots)
        return str(cwd / ".xpp-out" / task["execution_id"])

    def test_running_task_cancel_returns_output_dir(self):
        # (a) the runner has been launched, so the partial files live at the
        # directory it writes. The tool result must name that path.
        _, task, dep, turn = self._task("running")
        self.assertEqual(task["status"], "queued")
        task["status"] = "running"
        self.app.store.put("task", task)

        result = self.app.cancel_dependency("bot-main", turn["id"], dep["id"])

        self.assertEqual(result.get("status"), "cancelled")
        # Stored cwd is not symlink-resolved (/var/...). supervise() mkdirs
        # checked(cwd)/.xpp-out/<execution_id> (/private/var/...). Same directory.
        self.assertEqual(Path(result["output_dir"]).resolve(),
                         Path(self._runner_output_dir(task)).resolve())
        self.assertEqual(result.get("task_id"), task["id"])

    def test_pending_task_cancel_omits_output_dir(self):
        # (b) queued, never started. There is no directory to point at.
        _, task, dep, turn = self._task("pending")
        self.assertEqual(task["status"], "queued")
        self.assertNotIn(task["status"], {"running", "cancel_requested"})

        result = self.app.cancel_dependency("bot-main", turn["id"], dep["id"])

        self.assertEqual(result.get("status"), "cancelled")
        self.assertEqual(result.get("task_id"), task["id"])
        self.assertNotIn("output_dir", result)
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "cancelled")

    def test_child_work_cancel_unchanged(self):
        # (c) a child_work dependency is not a task. Same result as before.
        worker = self.app.create_bot({"name": "A", "role": "r"})
        sent = self.app.send("bot-main", {"text": "父"}, "child")
        handoff = self.app.delegate("bot-main", sent["work_id"], {
            "target_bot_id": worker["id"], "goal": "g", "request": "r", "acceptance": ["a"]})
        dep = self.app.store.find("dependency", source_work_id=sent["work_id"])[0]
        self.assertEqual(dep["kind"], "child_work")
        turn = self.app.store.get("turn", sent["turn_id"])
        turn["status"] = "running"
        self.app.store.put("turn", turn)

        result = self.app.cancel_dependency("bot-main", turn["id"], dep["id"])

        self.assertEqual(result, {"dependency_id": dep["id"], "status": "cancelled"})
        self.assertEqual(self.app.store.get("work", handoff["work_id"])["status"], "cancelled")

    def test_correction_clause_points_executor_at_output_dir(self):
        # (d) tier 3 still cancels first; after it returns, submit and name
        # output_dir in the task text. The executor reads that directory
        # inside its cwd and continues; without output_dir, redo.
        sent = self.app.send("bot-main", {"text": "另一件事", "allow_write": True}, "prompt")
        system = projection(self.app, self.app.store.get("turn", sent["turn_id"]))[0]["content"]
        self.assertIn(
            "tier 3: first cancel_dependency to withdraw the running task, keeping "
            "artifacts already produced; after it returns, submit_task to redo the "
            "remaining part under the new request, writing the returned output_dir "
            "(if any, inside the executor's working directory) into the task description "
            "so the executor reads them first, keeps the done part, and continues from "
            "the first unfinished part; with no output_dir, redo per the new request. "
            "Do not let the part that will be wrong keep running.",
            system)
        self.assertIn(
            "in the same batch first cancel_dependency the dependency created under "
            "the old understanding; if more is still wanted, submit_task per the new "
            "request after it returns.",
            system)
        # review: no clause may cancel a running task and resubmit in the
        # same batch; that batch cannot read the returned output_dir.
        self.assertNotIn("同一批里先 cancel_dependency 撤回正在跑的任务", system)
        self.assertNotIn("同一批里 cancel_dependency 撤回正在跑的任务", system)
        self.assertNotIn("作为 inputs", system)


if __name__ == "__main__":
    unittest.main()
