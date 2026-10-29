"""Post-merge gaps in.

Ownership is undetermined only while link_work can still rebind the current
Work. Readonly and checkpoint prose held during that window is written if the
Work stays, and dropped if the turn links away. dependency_forbidden names
link_work only while that rebind is still possible.

(a) fails on: the prose-less submit never calls deliver_interim, so the
earlier readonly prose is not stored.
(b) fails on: checkpoint prose clears entry_mode and link_work raises
work_not_provisional.
(c) fails on: the advice always names link_work, including after this
Work already has a side effect.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang.pi_runtime import PiModel
from pangpang.runtime import Runtime
from pangpang.service import Service
from pangpang.store import Fault


HELD = "我先看一下现在的状态。"
CHECKPOINT = "先把这一阶段记下来。"


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


def assistant_messages(app, work_id, content=None):
    rows = [m for m in app.store.find("message", work_id=work_id)
            if m.get("kind") == "assistant"]
    if content is None:
        return rows
    return [m for m in rows if m.get("content") == content]


class OwnershipRebindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _waiting_sibling(self, key):
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
        return sent

    def test_readonly_prose_persists_after_proseless_submit(self):
        # (a) batch1 prose is stored under msg-<turn>-<first call> once batch2
        # commits a task and stays on this Work. Replay does not write another.
        self.app.probe("codex-local")
        fresh = self.app.send("bot-main", {"text": "派一件活", "allow_write": True}, "a-hold")
        read = [tool("call-read", "catalog", {})]
        submit = [tool("call-sub", "submit_task", {"prompt": "整理工作目录里的笔记"})]
        runtime = Runtime(self.app, _Seq([
            {"role": "assistant", "content": HELD, "tool_calls": read},
            {"role": "assistant", "content": "", "tool_calls": submit},
        ]))
        runtime.run(self.app.store.get("turn", fresh["turn_id"]))
        message_id = "msg-%s-%s" % (fresh["turn_id"], "call-read")
        rows = assistant_messages(self.app, fresh["work_id"], HELD)
        self.assertEqual([m["id"] for m in rows], [message_id])
        self.assertEqual(len(self.app.store.find("task", work_id=fresh["work_id"])), 1)
        runtime.run(self.app.store.get("turn", fresh["turn_id"]))
        self.assertEqual(runtime.model.n, 2)
        self.assertEqual(
            [m["id"] for m in assistant_messages(self.app, fresh["work_id"], HELD)],
            [message_id])

    def test_pi_readonly_prose_persists_after_proseless_submit(self):
        # Same contract on the Pi loop, including a replay that must not
        # call the model or insert a second message.
        self.app.probe("codex-local")
        fresh = self.app.send("bot-main", {"text": "派一件活", "allow_write": True}, "a-pi")
        read = [tool("call-read", "catalog", {})]
        submit = [tool("call-sub", "submit_task", {"prompt": "整理工作目录里的笔记"})]
        seen = {"n": 0}

        def exchange(profile, history, tools, handler, **kwargs):
            seen["n"] += 1
            if seen["n"] > 1:
                raise AssertionError("replay must not call the model")
            handler({"type": "context"})
            handler({"type": "model_begin"})
            handler({"type": "assistant", "stop": "toolUse", "usage": {}, "message": {
                "role": "assistant", "content": HELD, "tool_calls": read}})
            handler({"type": "tool", "call_id": "call-read", "name": "catalog", "args": {}})
            handler({"type": "context"})
            handler({"type": "model_begin"})
            handler({"type": "assistant", "stop": "toolUse", "usage": {}, "message": {
                "role": "assistant", "content": "", "tool_calls": submit}})
            handler({"type": "tool", "call_id": "call-sub", "name": "submit_task",
                     "args": {"prompt": "整理工作目录里的笔记"}})
            return {"waiting": True}

        runtime = Runtime(self.app)
        message_id = "msg-%s-%s" % (fresh["turn_id"], "call-read")
        with patch.object(PiModel, "exchange", side_effect=exchange):
            runtime.run(self.app.store.get("turn", fresh["turn_id"]))
            self.assertEqual(
                [m["id"] for m in assistant_messages(self.app, fresh["work_id"], HELD)],
                [message_id])
            runtime.run(self.app.store.get("turn", fresh["turn_id"]))
        self.assertEqual(seen["n"], 1)
        self.assertEqual(
            [m["id"] for m in assistant_messages(self.app, fresh["work_id"], HELD)],
            [message_id])

    def test_readonly_after_side_effect_is_stored_while_entry_still_provisional(self):
        # A prose-less submit leaves entry_mode provisional, but the task means
        # link_work can never succeed. Later readonly prose must be stored.
        # An entry_mode gate that drops it was the prior bug.
        self.app.probe("codex-local")
        fresh = self.app.send("bot-main", {"text": "先派再看", "allow_write": True}, "a-later")
        self.app.submit_task("bot-main", fresh["work_id"], {"prompt": "整理工作目录里的笔记"})
        work = self.app.store.get("work", fresh["work_id"])
        self.assertEqual(work.get("entry_mode"), "provisional")
        self.assertTrue(self.app.store.find("task", work_id=fresh["work_id"], limit=1))
        read = [tool("call-read", "catalog", {})]
        Runtime(self.app, _Seq([
            {"role": "assistant", "content": HELD, "tool_calls": read},
            {"role": "assistant", "content": "看完了。"},
        ])).run(self.app.store.get("turn", fresh["turn_id"]))
        self.assertEqual(
            [m["id"] for m in assistant_messages(self.app, fresh["work_id"], HELD)],
            ["msg-%s-%s" % (fresh["turn_id"], "call-read")])

    def test_checkpoint_prose_does_not_block_link(self):
        # (b) checkpoint prose must not clear the entry. link_work then succeeds
        # and no assistant message stays on the entry Work.
        sibling = self._waiting_sibling("b-run")
        fresh = self.app.send("bot-main", {"text": "改去那件工作", "allow_write": True}, "b-new")
        checkpoint = [tool("call-ck", "checkpoint_work",
                           {"work_id": fresh["work_id"], "summary": "阶段"})]
        link = [tool("call-link", "link_work", {"work_id": sibling["work_id"]})]
        Runtime(self.app, _Seq([
            {"role": "assistant", "content": CHECKPOINT, "tool_calls": checkpoint},
            {"role": "assistant", "content": "接回那件工作。", "tool_calls": link},
        ])).run(self.app.store.get("turn", fresh["turn_id"]))
        turn = self.app.store.get("turn", fresh["turn_id"])
        linked = [r for r in tool_results(turn) if r.get("linked_from") or r.get("error")]
        self.assertTrue(linked)
        self.assertEqual(linked[-1].get("error"), None)
        self.assertEqual(linked[-1].get("linked_from"), fresh["work_id"])
        self.assertEqual(self.app.store.get("work", fresh["work_id"])["status"], "linked")
        self.assertEqual(turn["work_id"], sibling["work_id"])
        self.assertEqual(assistant_messages(self.app, fresh["work_id"]), [])
        self.assertEqual(assistant_messages(self.app, sibling["work_id"], CHECKPOINT), [])
        self.assertEqual(assistant_messages(self.app, sibling["work_id"], "接回那件工作。"), [])

    def test_cross_work_cancel_from_settled_work_does_not_name_link(self):
        # (c) the forbid stays. Once this Work has a side effect, the advice
        # must not point at link_work (that call would fail with no advice).
        self.app.probe("codex-local")
        sibling = self._waiting_sibling("c-sib")
        dep = self.app.store.find("dependency", source_work_id=sibling["work_id"])[0]
        fresh = self.app.send("bot-main", {"text": "这边已经派过了", "allow_write": True}, "c-new")
        self.app.submit_task("bot-main", fresh["work_id"], {"prompt": "整理笔记"})
        work = self.app.store.get("work", fresh["work_id"])
        self.assertEqual(work.get("entry_mode"), "provisional")
        self.assertTrue(self.app.store.find("task", work_id=fresh["work_id"], limit=1))
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
                advice = cm.exception.advice or ""
                self.assertNotIn("link_work", advice)
                self.assertIn("original Work", advice)
        self.assertEqual(self.app.store.get("dependency", dep["id"])["status"], "pending")

    def test_settled_work_delivers_readonly_prose_once(self):
        # A continued Work is not provisional, so held-prose flush has nothing
        # to do. Each readonly batch is deliver_interim'd once, at precommit,
        # and settle/finish must not walk the history and call it again.
        first = self.app.send("bot-main", {"text": "先说一句", "allow_write": True}, "hot-1")
        self.app.finish_turn(self.app.store.get("turn", first["turn_id"]), "先这样。")
        second = self.app.send(
            "bot-main", {"text": "再看几眼", "work_id": first["work_id"], "allow_write": True}, "hot-2")
        self.assertEqual(second["work_id"], first["work_id"])
        work = self.app.store.get("work", second["work_id"])
        self.assertNotEqual(work.get("entry_mode"), "provisional")
        prose = ["看了第一眼。", "看了第二眼。", "看了第三眼。"]
        replies = [
            {"role": "assistant", "content": text, "tool_calls": [tool("call-%d" % i, "catalog", {})]}
            for i, text in enumerate(prose)
        ]
        replies.append({"role": "assistant", "content": "三眼都看完了。"})
        with patch.object(Service, "deliver_interim", wraps=self.app.deliver_interim) as delivered:
            Runtime(self.app, _Seq(replies)).run(self.app.store.get("turn", second["turn_id"]))
        self.assertEqual(delivered.call_count, len(prose))
        for i, text in enumerate(prose):
            rows = assistant_messages(self.app, second["work_id"], text)
            self.assertEqual([m["id"] for m in rows], ["msg-%s-call-%d" % (second["turn_id"], i)])


if __name__ == "__main__":
    unittest.main()
