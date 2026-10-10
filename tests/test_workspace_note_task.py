"""A workspace note task is executor work. The notes are files already in
the executor workspace, not chat attachments. The planner is told to read
and save them through submit_task. A text reply that never starts the local
executor is not that work: the run must leave a task, a receipt, an artifact,
and action-plan.md."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_bot_runtime import call, config
from pangpang.runtime import Runtime
from pangpang.service import Service


NOTE = ("read the project notes already in the workspace, write a one-page "
        "action plan, save it as action-plan.md")
FOLLOW = "add a risk section"
REFUSAL = "citations and evidence are empty; please paste or upload the notes."

# Reads the notes already in cwd and writes action-plan.md. The follow-up
# prompt names the risk section; the first prompt does not.
EXECUTOR = r"""
import pathlib, sys
data = sys.stdin.read()
notes = []
for name in ("goals.txt", "risks.txt", "open.txt"):
    p = pathlib.Path(name)
    if p.is_file():
        notes.append(p.read_text(encoding="utf-8"))
plan = pathlib.Path("action-plan.md")
if "add a risk section" in data:
    prior = plan.read_text(encoding="utf-8") if plan.is_file() else ""
    plan.write_text(prior + "\n## Risk\nRISK-SECTION-FROM-EXECUTOR\n", encoding="utf-8")
else:
    plan.write_text("# Action plan\n" + "\n".join(notes) + "\n", encoding="utf-8")
print("executor-wrote-action-plan")
"""


# The prompt rule. Without it, a planner that follows the prompt refuses
# instead of calling the existing submit_task tool. (The canonical prompt is now
# English — see pangpang/planner_prompt.py.)
RULE = "use submit_task to have the executor read and write them"


class _Planner:
    """Follows the system prompt. Does not parse filenames on its own."""

    def __init__(self, text):
        self.text = text

    def complete(self, profile, messages, tools):
        system = messages[0]["content"]
        incoming = next(m for m in reversed(messages) if m["role"] == "user")["content"]
        if "execution_result" in incoming:
            return {"content": "行动计划已写好。"}, {}
        if NOTE in incoming:
            prompt = NOTE
        elif FOLLOW in incoming:
            prompt = FOLLOW
        else:
            return {"content": self.text}, {}
        if RULE in system:
            return call("submit_task", prompt=prompt, outputs=["action-plan.md"]), {}
        return {"content": REFUSAL}, {}


class WorkspaceNoteTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        (self.root / "goals.txt").write_text("ship the notes feature\n", encoding="utf-8")
        (self.root / "risks.txt").write_text("timeline is tight\n", encoding="utf-8")
        (self.root / "open.txt").write_text("no owner for rollout\n", encoding="utf-8")
        cfg = config(self.root)
        cfg["installations"][0]["argv"] = [sys.executable, "-c", EXECUTOR]
        # omitted --deployment-level. The flag is not this bug.
        self.app = Service(cfg, self.root.parent / "data")
        self.app.probe("codex-local")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _steps(self, runtime, limit=40):
        for _ in range(limit):
            runtime.step()

    def _result_notifications(self):
        return [n for n in self.app.store.find("notification") if n.get("kind") == "result"]

    def test_note_task_runs_local_executor_and_chat_reply_is_not_delivery(self):
        runtime = Runtime(self.app, _Planner(REFUSAL))
        sent = self.app.send("bot-main", {"text": NOTE}, "note")
        runtime.step()
        work = self.app.store.get("work", sent["work_id"])
        # The refusal ran no executor. It must not close the work.
        self.assertNotEqual(work["status"], "delivered")
        self.assertEqual(self._result_notifications(), [])
        tasks = self.app.store.find("task")
        self.assertEqual(len(tasks), 1)
        self.assertIn(NOTE, tasks[0]["user_prompt"])
        self.assertIn("action-plan.md", tasks[0].get("outputs") or [])

        for _ in range(40):
            runtime.step()
            task = self.app.store.get("task", tasks[0]["id"])
            if task.get("status") == "succeeded" and (self.root / "action-plan.md").is_file():
                break
            time.sleep(0.02)
        task = self.app.store.get("task", tasks[0]["id"])
        self.assertEqual(task["status"], "succeeded", task)
        receipt = task.get("receipt") or {}
        self.assertEqual(receipt.get("status"), "succeeded")
        self.assertTrue(receipt.get("artifacts"), receipt)
        self.assertEqual(len(self.app.store.find("artifact")), 1)
        plan = (self.root / "action-plan.md").read_text(encoding="utf-8")
        self.assertIn("ship the notes feature", plan)
        self.assertIn("timeline is tight", plan)
        self.assertIn("no owner for rollout", plan)
        self.assertEqual((self.root / "goals.txt").read_text(encoding="utf-8"), "ship the notes feature\n")
        self.assertEqual((self.root / "risks.txt").read_text(encoding="utf-8"), "timeline is tight\n")
        self.assertEqual((self.root / "open.txt").read_text(encoding="utf-8"), "no owner for rollout\n")

        for _ in range(20):
            if self.app.store.get("work", sent["work_id"])["status"] == "delivered":
                break
            runtime.step()
        self.assertEqual(self.app.store.get("work", sent["work_id"])["status"], "delivered")

        follow = self.app.send(
            "bot-main", {"text": FOLLOW, "work_id": sent["work_id"]}, "risk",
            routing={"work_id": sent["work_id"], "route_source": "explicit",
                     "route_quality": "deterministic"})
        self.assertEqual(follow["route_source"], "explicit")
        runtime.step()
        followed = self.app.store.get("work", follow["work_id"])
        self.assertNotEqual(followed["status"], "delivered")
        tasks = self.app.store.find("task")
        self.assertEqual(len(tasks), 2)
        second = []
        for _ in range(40):
            runtime.step()
            second = [t for t in self.app.store.find("task") if t["id"] != task["id"]]
            plan = (self.root / "action-plan.md").read_text(encoding="utf-8")
            if (len(second) == 1 and second[0]["status"] == "succeeded"
                    and "RISK-SECTION-FROM-EXECUTOR" in plan):
                break
            time.sleep(0.02)
        plan = (self.root / "action-plan.md").read_text(encoding="utf-8")
        self.assertIn("RISK-SECTION-FROM-EXECUTOR", plan)
        self.assertIn("ship the notes feature", plan)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["status"], "succeeded", second[0])
        self.assertTrue((second[0].get("receipt") or {}).get("artifacts"))
        self.assertGreaterEqual(len(self.app.store.find("artifact")), 1)

    def test_ordinary_reply_still_delivers_without_an_executor(self):
        runtime = Runtime(self.app, _Planner("在的。"))
        sent = self.app.send("bot-main", {"text": "hello"}, "hi")
        runtime.step()
        self.assertEqual(self.app.store.get("work", sent["work_id"])["status"], "delivered")
        self.assertEqual(self.app.store.find("task"), [])
        self.assertFalse((self.root / "action-plan.md").exists())
        # No executor ran, so the delivery notice claims a reply, not completion.
        titles = [n["title"] for n in self._result_notifications()]
        self.assertEqual(len(titles), 1, titles)
        self.assertTrue(titles[0].startswith("已回复："), titles)
        self.assertNotIn("已完成", titles[0])


if __name__ == "__main__":
    unittest.main()
