""": a running turn on the target still blocks link_work, and says so.

one_running_turn_per_work is unchanged. The fault carries advice because the
merge was not recorded: continue on the original Work, or resend after that
turn ends. A pending executor task is not a running turn.

(a) fails on: link_work returns target_busy with no advice.
(b) is the guard.
"""
import json
import tempfile
import unittest
from pathlib import Path

from test_bot_runtime import config
from pangpang.runtime import Runtime
from pangpang.service import Service


def tool(call_id, name, body):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(body)}}


class _Seq:
    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, profile, messages, tools):
        if not self.replies:
            return {"content": "停"}, {}
        return self.replies.pop(0), {}


def tool_results(turn):
    out = []
    for message in turn.get("history") or []:
        if message.get("role") != "tool":
            continue
        out.append(json.loads(message["content"]))
    return out


class Issue118TargetBusyAdviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _link(self, entry, target_work_id, replies):
        link = [tool("call-link", "link_work", {"work_id": target_work_id})]
        Runtime(self.app, _Seq(replies or [
            {"role": "assistant", "content": "接到那件工作。", "tool_calls": link},
        ])).run(self.app.store.get("turn", entry["turn_id"]))
        turn = self.app.store.get("turn", entry["turn_id"])
        linked = [r for r in tool_results(turn) if "error" in r or r.get("linked_from")]
        self.assertTrue(linked)
        return turn, linked[-1]

    def test_running_turn_link_returns_target_busy_with_advice(self):
        # (a) another running turn on the target. The check still refuses.
        # Advice names continuing on the original Work and waiting out the
        # turn, and does not claim the merge will happen by itself.
        target = self.app.send("bot-main", {"text": "正在进行的工作", "allow_write": True}, "busy-target")
        running = self.app.store.get("turn", target["turn_id"])
        running["status"] = "running"
        self.app.store.put("turn", running)
        entry = self.app.send("bot-main", {"text": "并到那件工作里", "allow_write": True}, "busy-entry")
        self.assertEqual(
            self.app.store.get("work", entry["work_id"]).get("entry_mode"), "provisional")
        self.assertNotEqual(entry["work_id"], target["work_id"])
        turn, result = self._link(entry, target["work_id"], [
            {"role": "assistant", "content": "接到那件工作。", "tool_calls": [
                tool("call-link", "link_work", {"work_id": target["work_id"]})]},
            {"role": "assistant", "content": "那边这一轮还没结束。"},
        ])
        self.assertEqual(result.get("error"), "target_busy")
        advice = result.get("advice") or ""
        self.assertTrue(advice.strip())
        self.assertIn("continue on the original Work", advice)
        self.assertIn("after that turn ends", advice)
        folded = advice.lower()
        for claim in ("automatically", "will retry", "will link", "will merge",
                      "will follow", "i will"):
            self.assertNotIn(claim, folded)
        self.assertNotEqual(self.app.store.get("work", entry["work_id"])["status"], "linked")
        self.assertEqual(self.app.store.get("turn", target["turn_id"])["status"], "running")
        self.assertEqual(turn["work_id"], entry["work_id"])

    def test_pending_task_without_running_turn_still_links(self):
        # (b) a waiting Work with a pending executor task and no running turn
        # is not target_busy. The task stays pending.
        self.app.probe("codex-local")
        target = self.app.send("bot-main", {"text": "整理笔记", "allow_write": True}, "task-target")
        submitted = self.app.submit_task(
            "bot-main", target["work_id"], {"prompt": "整理工作目录里的笔记"})
        held = self.app.store.get("turn", target["turn_id"])
        held["status"] = "done"
        self.app.store.put("turn", held)
        work = self.app.store.get("work", target["work_id"])
        self.assertEqual(work["status"], "waiting")
        self.assertIn(submitted["dependency_id"], work.get("pending_dep_ids") or [])
        dep = self.app.store.get("dependency", submitted["dependency_id"])
        self.assertEqual(dep["kind"], "task")
        self.assertEqual(dep["status"], "pending")
        self.assertNotEqual(self.app.store.get("task", submitted["task_id"])["status"], "running")
        self.assertFalse(any(
            t["status"] == "running"
            for t in self.app.store.find("turn", work_id=target["work_id"])))
        entry = self.app.send("bot-main", {"text": "补到那件工作", "allow_write": True}, "task-entry")
        self.assertEqual(
            self.app.store.get("work", entry["work_id"]).get("entry_mode"), "provisional")
        turn, result = self._link(entry, target["work_id"], None)
        self.assertIsNone(result.get("error"))
        self.assertEqual(result.get("linked_from"), entry["work_id"])
        self.assertEqual(self.app.store.get("work", entry["work_id"])["status"], "linked")
        self.assertEqual(turn["work_id"], target["work_id"])
        self.assertEqual(self.app.store.get("work", target["work_id"])["status"], "waiting")
        self.assertEqual(
            self.app.store.get("dependency", submitted["dependency_id"])["status"], "pending")

    def test_prompt_scopes_append_and_names_the_reading(self):
        # The system prompt already pins phrases this way (test_dispatch_prose_commit).
        # One assertion for the append/correct boundary; the first reply is the
        # reading plus not narrating another Work.
        fresh = self.app.send("bot-main", {"text": "另一件事", "allow_write": True}, "prompt")
        system = Runtime(self.app).context(
            self.app.store.get("turn", fresh["turn_id"]))[0]["content"]
        self.assertIn("not-yet-done part", system)
        self.assertIn("whether you read it as an addition, a correction, or a new matter", system)
        self.assertIn("do not report another Work's progress", system)
        self.assertIn("checkpoint_work optional", system)


if __name__ == "__main__":
    unittest.main()
