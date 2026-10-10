""": skip readonly precommit while a Work is still provisional, and tell
the model how to rebind before cancelling another Work's dependency.

(a) fails on: the precommit persists the prose and clears entry_mode,
so link_work raises work_not_provisional.
(b) fails on: dependency_forbidden has no advice.
(c) is green on both trees. finish_turn rejects link_work with
work_not_provisional. A committed submit_task rejects it with the code main
already uses for that path.
"""
import json
import tempfile
import unittest
from pathlib import Path

from test_bot_runtime import config
from pangpang.runtime import Runtime
from pangpang.service import Service
from pangpang.store import Fault


PROSE = "我先看一下那件正在进行的工作。"


def tool(call_id, name, body):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(body)}}


class _Seq:
    def __init__(self, replies):
        self.replies = list(replies)
        self.n = 0

    def complete(self, profile, messages, tools):
        self.n += 1
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


def assistant_messages(app, work_id, content):
    return [m for m in app.store.find("message", work_id=work_id)
            if m.get("kind") == "assistant" and m.get("content") == content]


class Issue116Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _waiting_sibling(self, key):
        """An in-progress Work with a pending dependency and no running turn."""
        sent = self.app.send("bot-main", {"text": "进行中的工作", "allow_write": True}, key)
        worker = self.app.create_bot({"name": "助手-" + key, "role": "r"})
        self.app.delegate("bot-main", sent["work_id"], {
            "target_bot_id": worker["id"], "goal": "g", "request": "r", "acceptance": ["a"]})
        turn = self.app.store.get("turn", sent["turn_id"])
        turn["status"] = "done"
        self.app.store.put("turn", turn)
        work = self.app.store.get("work", sent["work_id"])
        self.assertEqual(work["status"], "waiting")
        self.assertTrue(work.get("pending_dep_ids"))
        self.assertFalse(any(
            t["status"] == "running"
            for t in self.app.store.find("turn", work_id=sent["work_id"])))
        return sent

    def test_readonly_precommit_skipped_on_provisional_so_link_succeeds(self):
        # (a) redirect without continue. batch1 must not become an assistant
        # message on B; batch2 rebinds onto the waiting sibling.
        sibling = self._waiting_sibling("a-run")
        fresh = self.app.send("bot-main", {"text": "改去那件工作", "allow_write": True}, "a-new")
        self.assertEqual(
            self.app.store.get("work", fresh["work_id"]).get("entry_mode"), "provisional")
        read = [tool("call-read", "read_work", {"work_id": sibling["work_id"]})]
        link = [tool("call-link", "link_work", {"work_id": sibling["work_id"]})]
        Runtime(self.app, _Seq([
            {"role": "assistant", "content": PROSE, "tool_calls": read},
            {"role": "assistant", "content": "接回那件工作。", "tool_calls": link},
        ])).run(self.app.store.get("turn", fresh["turn_id"]))

        self.assertEqual(assistant_messages(self.app, fresh["work_id"], PROSE), [])
        self.assertEqual(self.app.store.get("work", fresh["work_id"])["status"], "linked")
        turn = self.app.store.get("turn", fresh["turn_id"])
        self.assertEqual(turn["work_id"], sibling["work_id"])
        self.assertTrue(any(
            isinstance(r, dict) and r.get("linked_from") == fresh["work_id"] and not r.get("error")
            for r in tool_results(turn)))

    def test_cross_work_cancel_forbidden_advice_names_link_work(self):
        # (b) the forbid stays. Both raise sites name link_work. The dependency
        # is not cancelled.
        sibling = self._waiting_sibling("b-run")
        dep = self.app.store.find("dependency", source_work_id=sibling["work_id"])[0]
        fresh = self.app.send("bot-main", {"text": "别在这边取消", "allow_write": True}, "b-new")
        turn = self.app.store.get("turn", fresh["turn_id"])
        runtime = Runtime(self.app, _Seq([]))
        cases = {
            "service": lambda: self.app.cancel_dependency("bot-main", turn["id"], dep["id"]),
            "preflight": lambda: runtime._semantic_preflight(
                turn, "cancel_dependency", {"dep_id": dep["id"]}),
        }
        for name, call in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(Fault) as cm:
                    call()
                self.assertEqual(cm.exception.code, "dependency_forbidden")
                self.assertIn("link_work", cm.exception.advice or "")
        self.assertEqual(self.app.store.get("dependency", dep["id"])["status"], "pending")

    def test_finish_turn_rejects_link(self):
        # (c) finish_turn still ends provisional.
        sibling = self._waiting_sibling("f-run")
        fresh = self.app.send("bot-main", {"text": "直接回答", "allow_write": True}, "f-new")
        self.app.finish_turn(self.app.store.get("turn", fresh["turn_id"]), "答完了")
        with self.assertRaises(Fault) as cm:
            self.app.link_work(fresh["turn_id"], sibling["work_id"])
        self.assertEqual(cm.exception.code, "work_not_provisional")

    def test_committed_submit_rejects_link(self):
        # (c) A yielded submit_task whose prose is stored still rejects link_work.
        # Main enforces work_not_provisional on this path: deliver_interim clears
        # entry_mode when that committed prose is stored.
        self.app.probe("codex-local")
        sibling = self._waiting_sibling("c-run")
        fresh = self.app.send("bot-main", {"text": "派一件活", "allow_write": True}, "c-new")
        calls = [tool("call-sub", "submit_task", {"prompt": "整理工作目录里的笔记"})]
        Runtime(self.app, _Seq([
            {"role": "assistant", "content": "我派下去了。", "tool_calls": calls},
        ])).run(self.app.store.get("turn", fresh["turn_id"]))
        self.assertEqual(len(self.app.store.find("task", work_id=fresh["work_id"])), 1)
        with self.assertRaises(Fault) as cm:
            self.app.link_work(fresh["turn_id"], sibling["work_id"])
        self.assertEqual(cm.exception.code, "work_not_provisional")
